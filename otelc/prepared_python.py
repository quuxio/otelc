"""Public prepare/command recipe for an owned native Python application/SDK pair."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ci import build_python_inputs as tooling
from otelc.config import Policy
from otelc.file_inputs import identity
from otelc import python as frontend
from otelc import python_launch_files as files
from otelc import python_recipe as recipe

ROOT = Path(__file__).resolve().parents[1]
REQUEST = 'python-request.json'
PLAN = 'python-resolved-plan.json'
PREPARED = 'python-prepared.json'


def protocols(*, profile: str = recipe.INPUT_PROFILE) -> dict[str, Any]:
    """Describe readable/written formats without preparing or launching an app.

    This handshake is format metadata, not source closure, toolchain support,
    successful export or independent qualification of a provider.
    """
    selected = recipe.version(profile)
    result = {'schema_version': selected, 'kind': 'otelc.python.protocols',
            'request': selected, 'plan': selected, 'prepared': selected, 'command': selected,
            'execution': selected,
            'journal': {'schema_version': selected, 'record_kinds': list(range(1, 8 if selected == 2 else 5))},
            'code': {'written': 2, 'readable': [2]},
            'health': 2, 'sdk': 1, 'code_comparisons': 1,
            'source_complete': False}
    if selected == 2:
        result['profile'] = profile
        result['origin_scope'] = 'native-entry-and-application-root-source-loaders'
        # The separate post-run comparison API currently accepts journal 1.
        result['code_comparisons'] = None
    return result


def digest(value: dict[str, Any]) -> str:
    """Bind canonical complete requests rather than selected policy fragments."""
    return hashlib.sha256(recipe.encoded(value).encode()).hexdigest()


def native_tool(profile: str = recipe.INPUT_PROFILE) -> Path:
    """Recompute the fixed tool namespace and verify existing native products."""
    recipe.version(profile)
    inputs, flags = tooling.toolchain(origin_entry=profile == recipe.ORIGIN_PROFILE)
    if profile == recipe.ORIGIN_PROFILE and not inputs['origin_chain']['available']:
        raise ValueError('native Python origins are unavailable for the selected image')
    inputs.update(flags=flags, coverage=False)
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return tooling.verify(ROOT / 'build/tools/python-inputs' / key, inputs)


def manifest(original: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Derive maintained tooling/entry identities without executing app code."""
    driver = native_tool(original['profile'])
    maintained = [ROOT / 'bin/quux-otelc', *sorted((ROOT / 'otelc').glob('*.py')),
                  ROOT / 'ci/build_python_inputs.py', ROOT / 'ci/native_encoder.py',
                  ROOT / 'ci/native_origin_chain.py', ROOT / 'requirements.txt',
                  ROOT / 'requirements-dev.txt']
    entry = original['paths'].get('entry')
    return {'schema_version': recipe.version(original['profile']), 'kind': 'otelc.python.prepared',
            'request_sha256': digest(original), 'plan_sha256': digest(plan),
            'driver': identity(driver), 'build_manifest': identity(driver.parent / 'build.json'),
            'producer': [identity(path) for path in maintained],
            'entry': identity(Path(entry['resolved'])) if entry is not None else None,
            'source_complete': False}


def prepare(policy: Policy, runtime: Path | str, *, root: Path | str,
            entry: Path | str | None = None, module: str | None = None,
            arguments: tuple[str, ...] = (), plain: bool = False,
            profile: str = recipe.INPUT_PROFILE) -> dict[str, Any]:
    """Prepare fixed tooling and metadata without starting an application or SDK."""
    selected = recipe.version(profile)
    application = Path(root).resolve(strict=True)
    frontend.validate_policy(policy)
    frontend.invocation(application, entry=Path(entry) if entry is not None else None,
                        module=module, arguments=arguments)
    directory = Path(runtime)
    directory.mkdir(mode=0o700)
    directory = files.private_directory(directory)
    original = recipe.request(policy, runtime, root=root, entry=entry, module=module,
                              arguments=arguments, plain=plain, profile=profile)
    plan = recipe.resolve_plan(original)
    recipe.verify(original, plan)
    tooling.build(origin_entry=profile == recipe.ORIGIN_PROFILE)
    prepared = manifest(original, plan)
    files.write(directory / REQUEST, original)
    files.write(directory / PLAN, plan)
    # This last file is the completed-prepare marker. A partial preparation
    # remains refused; it is never repaired by silently overwriting its files.
    files.write(directory / PREPARED, prepared)
    return {'schema_version': selected, 'kind': 'otelc.python.command',
            'request': original, 'plan': plan, 'prepared': prepared,
            'command': invocation(directory), 'source_complete': False}


def invocation(runtime: Path) -> list[str]:
    """The sole supervisor command has a fixed interpreter, script and option set."""
    return [sys.executable, '-I', '-S', '-B', str(ROOT / 'otelc/python_supervisor.py'),
            '--runtime', str(runtime.resolve(strict=True))]


def verify(runtime: Path, *, environment: dict[str, str] | None = None
           ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Independently rederive all prepared files; no receipt commands are executed."""
    directory = files.private_directory(runtime)
    original, request_bytes = files.read(directory / REQUEST)
    plan, plan_bytes = files.read(directory / PLAN)
    prepared, prepared_bytes = files.read(directory / PREPARED)
    recipe.verify(original, plan, environment=environment)
    if Path(plan['runtime']) != directory:
        raise ValueError('Python prepared runtime differs from the requested directory')
    expected = manifest(original, plan)
    if (request_bytes != recipe.encoded(original).encode()
            or plan_bytes != recipe.encoded(recipe.resolve_plan(original)).encode()
            or prepared_bytes != recipe.encoded(expected).encode() or prepared != expected):
        raise ValueError('Python prepared files differ from the fixed recipe or inputs')
    return original, plan, prepared


def command(policy: Policy, runtime: Path | str, *, root: Path | str,
            entry: Path | str | None = None, module: str | None = None,
            arguments: tuple[str, ...] = (), plain: bool = False,
            profile: str = recipe.INPUT_PROFILE) -> list[str]:
    """Prepare and return the derived supervisor command, without launching it."""
    prepared = prepare(policy, runtime, root=root, entry=entry, module=module,
                       arguments=arguments, plain=plain, profile=profile)
    return prepared['command']


def run(policy: Policy, runtime: Path | str, *, root: Path | str,
        entry: Path | str | None = None, module: str | None = None,
        arguments: tuple[str, ...] = (), plain: bool = False,
        prepare_only: bool = False, profile: str = recipe.INPUT_PROFILE) -> int:
    """Keep an owned process group through app completion and independent SDK drain."""
    prepared = prepare(policy, runtime, root=root, entry=entry, module=module,
                       arguments=arguments, plain=plain, profile=profile)
    if prepare_only:
        print(json.dumps(prepared, indent=2))
        return 0
    process = subprocess.Popen(prepared['command'], start_new_session=True)
    try:
        return process.wait()
    finally:
        # A crashed supervisor can leave live descendants holding the app's
        # channels. Ownership of this session survives its leader's exit.
        stop_group(process)


def stop_group(process: subprocess.Popen[bytes]) -> None:
    """Stop only this explicitly owned session if the caller is interrupted."""
    if not _signal_group(process.pid, signal.SIGTERM):
        process.wait(timeout=3)
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        process.poll()  # Reap a completed group leader while children finish.
        if not _signal_group(process.pid, 0):
            break
        time.sleep(.01)
    else:
        _signal_group(process.pid, signal.SIGKILL)
    process.wait(timeout=3)


def _signal_group(group: int, number: int) -> bool:
    """Distinguish a vanished macOS group from a denied live-group signal.

    A real killed-supervisor reproduction returned EPERM for signal zero, then
    an OS query showed no group members. Never forgive EPERM without evidence
    of absence, or signal any process outside the explicitly owned group.
    """
    try:
        os.killpg(group, number)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        if sys.platform != 'darwin':
            raise
        groups = subprocess.check_output(
            ['/bin/ps', '-A', '-o', 'pgid='], text=True, timeout=1).split()
        if not groups or any(not value.isascii() or not value.isdecimal() for value in groups):
            raise ValueError('owned process-group membership is unavailable')
        if group in map(int, groups):
            raise
        return False
