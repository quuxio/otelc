"""Generate source-free Node launches from the canonical common policy."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
import time
from pathlib import Path
from typing import Any

from otelc.config import Policy
from otelc import node_provenance, node_recipe

ROOT = Path(__file__).resolve().parent.parent


def protocols() -> dict[str, Any]:
    """Describe producer protocols without launching Node or reading app inputs."""
    return {'schema_version': 1, 'kind': 'otelc.node.protocols',
            'source': node_provenance.SOURCE_EVIDENCE_SCHEMA,
            'execution': 1, 'request': 1, 'preload': 1, 'sdk': 1}


def executable() -> str:
    """Select a supported installed Node runtime without relying on an older login PATH."""
    homebrew = Path('/opt/homebrew/opt/node@24/bin/node')
    selected = os.environ.get('OTELC_NODE') or (str(homebrew) if homebrew.is_file() else shutil.which('node'))
    if not selected:
        raise ValueError('Node 24.11+ is required')
    # Introspection is producer work; application preloads belong to the real launch only.
    environment = node_recipe.tooling_environment()
    version = subprocess.check_output([selected, '-p', 'process.versions.node'], text=True, env=environment).strip()
    if tuple(map(int, version.split('.'))) < (24, 11, 0):
        raise ValueError('Node 24.11+ is required')
    return str(Path(shutil.which(selected) or selected).resolve(strict=True))


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
            gc_fixture: bool = False, arguments: tuple[str, ...] = ()) -> list[str]:
    """Launch ordinary original URLs; compiler outputs and bootstrap are producer-owned."""
    selected = executable()
    requested_entry = entry or ROOT / 'examples' / language / ('BenchmarkApp.ts' if language == 'typescript' else 'BenchmarkApp.js')
    application = requested_entry.resolve(strict=True)
    plan = prepare(policy, runtime, language, root=root, backend=backend, plain=plain)
    original = node_recipe.request(policy, runtime, language, root=root, entry=requested_entry,
        selected=selected, backend=backend, plain=plain, gc_fixture=gc_fixture, arguments=arguments)
    runtime = Path(plan['runtime']['directory'])
    if node_recipe.resolve_plan(original, plan['launch_id']) != plan:
        raise ValueError('Node requested policy does not resolve to its prepared plan')
    prefix = 'node-plain' if plain else 'node'
    request_file = runtime / (prefix + '-request.json')
    write_generated(runtime, request_file.name, node_recipe.encoded(original))
    invocation = node_recipe.invocation(original)
    if plain and language == 'javascript':
        manifest = node_provenance.capture(ROOT, application, selected, plan, [request_file], invocation, original)
        node_recipe.verify(runtime, original, manifest, producer=ROOT)
        write_generated(runtime, 'node-plain-launch.json', json.dumps(manifest, sort_keys=True) + '\n')
        return invocation
    if not plain:
        os.mkfifo(runtime / 'node-events.fifo', 0o600)
    plan_file = runtime / (prefix + '-plan.json')
    bootstrap = runtime / (prefix + '-bootstrap.cjs')
    write_generated(runtime, plan_file.name, node_recipe.encoded(plan))
    write_generated(runtime, bootstrap.name, node_recipe.bootstrap_text(ROOT, runtime.resolve(), plain=plain))
    manifest = node_provenance.capture(ROOT, application, selected, plan, [request_file, plan_file, bootstrap], invocation, original)
    node_recipe.verify(runtime, original, manifest, producer=ROOT)
    write_generated(runtime, prefix + '-launch.json', json.dumps(manifest, sort_keys=True) + '\n')
    return invocation


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


def run(policy: Policy, runtime: Path, language: str, *, entry: Path | None = None,
        root: Path = ROOT, backend: str = 'native', plain: bool = False,
        arguments: tuple[str, ...] = ()) -> int:
    """Keep generated inputs alive through SDK drain and retain application failures."""
    invocation = command(policy, runtime, language, entry=entry, root=root,
                         backend=backend, plain=plain, arguments=arguments)
    prefix = 'node-plain' if plain else 'node'
    manifest = json.loads((runtime / (prefix + '-launch.json')).read_text())
    if not plain:
        print(f'Control descriptor: {runtime.resolve()}/control.json', file=sys.stderr)
    result = subprocess.call(invocation)
    if not plain:
        try:
            report = wait_report(runtime, timeout=policy.data['export']['shutdown_timeout_ms'] / 1000 + 2.)
            if report['phase'] != 'closed':
                print(f'Node SDK drain failed: {runtime.resolve()}/node-status.json', file=sys.stderr)
                result = result or 1
        except TimeoutError:
            print(f'Node SDK final report unavailable: {runtime.resolve()}/node-status.json', file=sys.stderr)
            result = result or 1
    try:
        verified = node_provenance.finish(runtime, manifest)
    except OSError:
        print(f'Node launch input evidence could not be written: {runtime.resolve()}/{prefix}-inputs.json', file=sys.stderr)
        return result or 1
    if not verified:
        print(f'Node launch input verification failed: {runtime.resolve()}/{prefix}-inputs.json', file=sys.stderr)
        result = result or 1
    return result
