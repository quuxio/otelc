"""Generate source-free Node launches from the canonical common policy."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import uuid
import time
from pathlib import Path
from typing import Any

from otelc.config import Policy

ROOT = Path(__file__).resolve().parent.parent


def executable() -> str:
    """Select a supported installed Node runtime without relying on an older login PATH."""
    homebrew = Path('/opt/homebrew/opt/node@24/bin/node')
    selected = os.environ.get('OTELC_NODE') or (str(homebrew) if homebrew.is_file() else shutil.which('node'))
    if not selected:
        raise ValueError('Node 24.11+ is required')
    version = subprocess.check_output([selected, '-p', 'process.versions.node'], text=True).strip()
    if tuple(map(int, version.split('.'))) < (24, 11, 0):
        raise ValueError('Node 24.11+ is required')
    return selected


def write_generated(directory: Path, filename: str, data: str) -> None:
    """Create an owned generated file exclusively; never replace application/runtime files."""
    descriptor = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w', closefd=False) as output:
            output.write(data)
    finally:
        os.close(descriptor)


def prepare(policy: Policy, runtime: Path, language: str, *, root: Path = ROOT,
            backend: str = 'native', plain: bool = False) -> dict[str, Any]:
    """Generate backend intent; the user still owns only the common TOML policy."""
    if language not in ('javascript', 'typescript') or language not in policy.data['languages']:
        raise ValueError('the selected Node language is not enabled in this policy')
    if backend not in ('native', 'api'):
        raise ValueError('unknown Microsoft TypeScript compiler profile')
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = runtime.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ValueError('Node runtime directory must be owned and mode 0700')
    root = root.resolve(strict=True)
    return {**policy.data, 'language': language, 'backend': backend, 'plain': plain, 'launch_id': uuid.uuid4().hex,
        'annotations': {'read_existing': False, 'inject_generated': False},
        'source_matchers': policy.data['sources'], 'function_matchers': policy.data['functions'],
        'lifetime_matchers': {key: policy.data['lifetimes'][key] for key in ('include', 'exclude')},
        'runtime': {'root': str(root), 'directory': str(runtime.resolve()),
                    'max_functions': 4096, 'max_active_calls': 4096}}


def command(policy: Policy, runtime: Path, language: str, *, plain: bool = False,
            entry: Path | None = None, root: Path = ROOT, backend: str = 'native',
            gc_fixture: bool = False) -> list[str]:
    """Launch ordinary original URLs; compiler outputs and bootstrap are producer-owned."""
    selected = executable()
    application = (entry or ROOT / 'examples' / language / ('BenchmarkApp.ts' if language == 'typescript' else 'BenchmarkApp.js')).resolve(strict=True)
    plan = prepare(policy, runtime, language, root=root, backend=backend, plain=plain)
    flags = ['--enable-source-maps', *(['--expose-gc'] if gc_fixture else [])]
    if plain and language == 'javascript':
        return [selected, *flags, str(application)]
    if not plain:
        os.mkfifo(runtime / 'node-events.fifo', 0o600)
    prefix = 'node-plain' if plain else 'node'
    plan_file = runtime / (prefix + '-plan.json')
    bootstrap = runtime / (prefix + '-bootstrap.mjs')
    target = ROOT / 'adapters/node/src' / ('plain-source-loader.ts' if plain else 'source-bootstrap.ts')
    write_generated(runtime, plan_file.name, json.dumps(plan, sort_keys=True) + '\n')
    write_generated(runtime, bootstrap.name,
        'process.env.OTELC_NODE_PLAN = ' + json.dumps(str(plan_file.resolve())) + ';\n'
        'await import(' + json.dumps(target.as_uri()) + ');\n')
    return [selected, *flags, '--import', str(bootstrap), str(application)]


def wait_report(runtime: Path, timeout: float = 15.) -> dict[str, Any]:
    """Wait for the independent SDK drain after the application process has exited."""
    filename = runtime / 'node-status.json'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = filename.read_text()
        except FileNotFoundError:
            data = ''
        if data:
            try:
                report = json.loads(data)
            except json.JSONDecodeError:
                # The owned FD can be in its final bounded write, never treat partial bytes as success.
                report = None
            if isinstance(report, dict) and report.get('phase') in ('closed', 'failed'):
                return report
        time.sleep(.01)
    raise TimeoutError('independent Node SDK final report was not completed')
