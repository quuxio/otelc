"""Fixed, compiler-free Rust preparation and explicit pre-exec host release."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from typing import Any

from otelc import health, native_gate, native_launch, rust_build, rust_compiler, rust_launch, rust_owned_build
from otelc import python_launch_files as files
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]
PROFILE = rust_owned_build.PROFILE
REQUEST, PLAN, PREPARED = 'rust-request.json', 'rust-plan.json', 'rust-prepared.json'
REQUEST_FIELDS = {'schema_version', 'kind', 'profile', 'build', 'runtime', 'policy',
                  'arguments', 'plain', 'wait_ms', 'launch_id'}


def protocols(*, source_build_version: int = 1) -> dict[str, Any]:
    """Negotiate the owned standalone formats without compiler/application execution."""
    if type(source_build_version) is not int or source_build_version not in (1, 2):
        raise ValueError('Rust protocols require explicit source build1 or2')
    lifetime = source_build_version == 2
    return {'schema_version': 1, 'kind': 'otelc.rust.protocols', 'profile': PROFILE,
            'owned_build': 1, 'source_build': source_build_version,
            'source_profile': rust_build.LIFETIME_PROFILE if lifetime else rust_build.PROFILE,
            'request': 1, 'plan': 1, 'prepared': 1, 'command': 1, 'execution': 1,
            'host_release': 1, 'gate': 1, 'native_plan': 3 if lifetime else 2,
            'health': 5 if lifetime else 1, 'embedded_sdk': True, 'source_complete': False}


def same(value: Any, expected: Any) -> bool:
    """Distinguish booleans, integers and floats in closed wire records."""
    return json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)


def producer() -> list[dict[str, Any]]:
    """Hold the fixed Python implementation; metadata does not select modules."""
    return [native_gate.identity(path) for path in sorted((ROOT / 'otelc').glob('*.py'))]


def outside(runtime: Path, owned: rust_owned_build.Build) -> None:
    """Keep runtime writes outside original, retained and owned build inputs."""
    rust_owned_build.outside(runtime, owned.source, owned.source.manifest())
    if rust_compiler.physical_within(runtime, owned.directory):
        raise ValueError('Rust runtime must be outside the owned build')


def request(policy: Policy, runtime: Path, build: dict[str, Any], arguments: tuple[str, ...],
            plain: bool, wait_ms: int, launch_id: str) -> dict[str, Any]:
    """Application arguments and wait budgets are literal bounded values."""
    if (type(arguments) is not tuple or len(arguments) > 4096
            or any(type(arg) is not str or '\0' in arg for arg in arguments)
            or sum(len(os.fsencode(arg)) for arg in arguments) > 1024 * 1024
            or type(plain) is not bool or type(wait_ms) is not int or not 1 <= wait_ms <= 300000
            or type(launch_id) is not str or len(launch_id) != 32
            or any(char not in '0123456789abcdef' for char in launch_id)):
        raise ValueError('invalid Rust arguments, lane, host budget or launch identity')
    return {'schema_version': 1, 'kind': 'otelc.rust.request', 'profile': PROFILE,
            'runtime': str(runtime), 'build': build, 'policy': policy.data,
            'arguments': list(arguments), 'plain': plain, 'wait_ms': wait_ms, 'launch_id': launch_id}


def plan(original: dict[str, Any], built: rust_build.Build) -> dict[str, Any]:
    """Derive the two fixed image paths; never use recorded executable argv."""
    name = 'plain' if original['plain'] else 'instrumented'
    binary = built.directory / name
    return {'schema_version': 1, 'kind': 'otelc.rust.plan', 'profile': PROFILE,
            'request_sha256': native_launch.digest(original), 'cwd': built.manifest()['root'],
            'argv': [str(binary), *original['arguments']], 'image': native_gate.identity(binary),
            'source_complete': False}


def manifest(original: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
    """Bind the fixed interpreter/producer, not a command selected by metadata."""
    return {'schema_version': 1, 'kind': 'otelc.rust.prepared', 'profile': PROFILE,
            'request_sha256': native_launch.digest(original), 'plan_sha256': native_launch.digest(resolved),
            'interpreter': native_gate.identity(Path(sys.executable).resolve(strict=True)),
            'interpreter_spelling': sys.executable, 'producer': producer(),
            'source_complete': False}


def prepare(policy: Policy, runtime: Path, *, build_directory: Path,
            arguments: tuple[str, ...] = (), plain: bool = False, wait_ms: int = 30000) -> dict[str, Any]:
    """Prepare one new single-use runtime without starting any process."""
    owned = rust_owned_build.read(build_directory, policy)
    built = owned.source
    runtime = runtime.resolve()
    outside(runtime, owned)
    original = request(policy, runtime, native_gate.identity(owned.directory / rust_owned_build.MANIFEST),
                       arguments, plain, wait_ms, secrets.token_hex(16))
    resolved = plan(original, built)
    prepared = manifest(original, resolved)
    runtime.mkdir(mode=0o700)
    for name, value in ((REQUEST, original), (PLAN, resolved), (PREPARED, prepared)):
        files.write(runtime / name, value)
    return prepared


def verify(runtime: Path) -> tuple[dict[str, Any], dict[str, Any], rust_build.Build, native_gate.Product]:
    """Recheck intent and derive its command without querying installed tools."""
    runtime = files.private_directory(runtime)
    original, _ = files.read(runtime / REQUEST)
    if (type(original) is not dict or set(original) != REQUEST_FIELDS or not same(original.get('schema_version'), 1)
            or type(original['build']) is not dict or type(original['build'].get('path')) is not str
            or type(original['arguments']) is not list or type(original['policy']) is not dict):
        raise ValueError('invalid Rust request format')
    build_path = Path(original['build']['path'])
    if (build_path.name != rust_owned_build.MANIFEST or not build_path.is_absolute()
            or build_path.resolve(strict=True) != build_path):
        raise ValueError('Rust request must bind its fixed build manifest')
    policy = Policy.parse(original['policy'])
    owned = rust_owned_build.read(build_path.parent, policy)
    outside(runtime, owned)
    built, gate = owned.source, owned.gate
    expected = request(policy, runtime, native_gate.identity(build_path), tuple(original['arguments']),
                       original['plain'], original['wait_ms'], original['launch_id'])
    resolved, _ = files.read(runtime / PLAN)
    prepared, _ = files.read(runtime / PREPARED)
    if (not same(original, expected) or not same(resolved, plan(original, built))
            or not same(prepared, manifest(original, resolved))):
        raise ValueError('Rust prepared intent, producer or fixed command changed')
    return original, resolved, built, gate


def command(runtime: Path) -> list[str]:
    """Select only the installed supervisor with isolated/no-site startup."""
    verify(runtime)
    if (runtime / 'rust-supervisor-start.json').exists():
        raise ValueError('Rust prepared execution is single-use')
    return [sys.executable, '-I', '-S', str(ROOT / 'otelc/rust_supervisor.py'), str(runtime)]


def application(value: dict[str, Any], original: dict[str, Any], resolved: dict[str, Any],
                supervisor_pid: int) -> dict[str, Any]:
    """Validate the closed waiting-child record, including exact integer IDs."""
    pid = value.get('pid')
    if type(pid) is not int or pid <= 1:
        raise ValueError('Rust application requires an integer process identity')
    expected = {'schema_version': 1, 'kind': 'otelc.rust.application', 'pid': pid,
                'parent_pid': supervisor_pid, 'process_group': pid, 'session': pid,
                'supervisor_pid': supervisor_pid, 'launch_id': original['launch_id'],
                'image': resolved['image'], 'phase': 'waiting_exec', 'source_complete': False}
    if not same(value, expected):
        raise ValueError('Rust application does not match the owned waiting process')
    return value


def release(runtime: Path, *, pid: int, supervisor_pid: int) -> None:
    """Publish one explicit acknowledgement after the host has verified ownership."""
    original, resolved, _, _ = verify(runtime)
    start, _ = files.read(runtime / 'rust-application-start.json')
    if (type(pid) is not int or type(supervisor_pid) is not int or pid <= 1 or supervisor_pid <= 1
            or not same(start.get('pid'), pid)):
        raise ValueError('Rust release does not identify the owned application')
    application(start, original, resolved, supervisor_pid)
    files.write(runtime / 'rust-host-release.json', {'schema_version': 1, 'kind': 'otelc.rust.host-release',
        'launch_id': original['launch_id'], 'pid': pid, 'supervisor_pid': supervisor_pid})


def validate_health(value: dict[str, Any], *, built: rust_build.Build,
                    pid: int, policy: dict[str, Any]) -> None:
    """Bind a final SDK checkpoint to the verified build and launched process."""
    rust_launch.validate_health(value, built, Policy.parse(policy), pid)


def execution(runtime: Path, *, supervisor_pid: int, parent_pid: int, returncode: int) -> dict[str, Any]:
    """Bind completed process observations; release alone is not image proof."""
    original, resolved, built, _ = verify(runtime)
    value, _ = files.read(runtime / 'rust-execution.json')
    start, _ = files.read(runtime / 'rust-supervisor-start.json')
    expected_start = {'schema_version': 1, 'kind': 'otelc.rust.supervisor', 'pid': supervisor_pid,
                      'parent_pid': parent_pid, 'process_group': supervisor_pid,
                      'session': supervisor_pid, 'launch_id': original['launch_id']}
    fields = {'schema_version', 'kind', 'profile', 'request_sha256', 'plan_sha256',
              'supervisor', 'application', 'released', 'process_exit_code', 'supervisor_exit_code',
              'exec_verified', 'sdk_health', 'inputs_rechecked', 'failures', 'source_complete'}
    if (any(type(pid) is not int or pid <= 1 for pid in (supervisor_pid, parent_pid))
            or type(returncode) is not int or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['kind'] != 'otelc.rust.execution' or value['profile'] != PROFILE
            or value['exec_verified'] is not False or value['source_complete'] is not False
            or type(value['released']) is not bool or type(value['inputs_rechecked']) is not bool
            or value['process_exit_code'] is not None and type(value['process_exit_code']) is not int
            or type(value['failures']) is not dict
            or any(type(key) is not str or type(reason) is not str for key, reason in value['failures'].items())
            or not same(start, expected_start)
            or not same(value.get('supervisor'), start) or not same(value.get('supervisor_exit_code'), returncode)
            or value.get('request_sha256') != native_launch.digest(original)
            or value.get('plan_sha256') != native_launch.digest(resolved)):
        raise ValueError('Rust execution does not match the owned supervisor and request')
    app = value['application']
    if app is not None:
        if type(app) is not dict:
            raise ValueError('invalid Rust application observation')
        application(app, original, resolved, supervisor_pid)
        observed, _ = files.read(runtime / 'rust-application-start.json')
        if not same(observed, app):
            raise ValueError('Rust execution changed its application observation')
    if value['released'] and (app is None or value['process_exit_code'] is None):
        raise ValueError('Rust release requires an observed child and exit')
    if value['released']:
        acknowledgement, _ = files.read(runtime / 'rust-host-release.json')
        expected_release = {'schema_version': 1, 'kind': 'otelc.rust.host-release',
                            'launch_id': original['launch_id'], 'pid': app['pid'],
                            'supervisor_pid': supervisor_pid}
        if not same(acknowledgement, expected_release):
            raise ValueError('Rust execution changed its host release')
    checkpoint = value['sdk_health']
    if original['plain'] and any((runtime / 'app' / name).exists() for name in ('plan.json', 'health.json')):
        raise ValueError('Rust plain execution contains SDK configuration or health')
    if checkpoint is not None:
        if original['plain'] or app is None:
            raise ValueError('Rust SDK health requires an instrumented application')
        validate_health(checkpoint, built=built, pid=app['pid'], policy=original['policy'])
        observed = health.read(runtime / 'app/health.json')
        if not same(checkpoint, observed):
            raise ValueError('Rust SDK health does not match the launched application')
    if not value['failures'] and (not value['released'] or not value['inputs_rechecked']
                                 or not original['plain'] and checkpoint is None):
        raise ValueError('Rust execution lacks its final observations')
    if value['failures'] and returncode == 0:
        raise ValueError('Rust failed execution cannot have a successful supervisor exit')
    if not value['failures']:
        child_exit = value['process_exit_code']
        expected_exit = 128 - child_exit if child_exit < 0 else child_exit
        if returncode != expected_exit:
            raise ValueError('Rust child exit contradicts the observed supervisor exit')
    return value


def run(policy: Policy, runtime: Path, *, build_directory: Path,
        arguments: tuple[str, ...] = (), plain: bool = False, prepare_only: bool = False) -> int:
    """Use the ordinary CLI as the release host, or leave release to a consumer."""
    value = prepare(policy, runtime, build_directory=build_directory, arguments=arguments, plain=plain)
    if prepare_only:
        print(json.dumps(value, indent=2))
        return 0
    process = subprocess.Popen(command(runtime), start_new_session=True)
    app = None
    owned_pid = None
    try:
        deadline = time.monotonic() + 30
        while app is None:
            if process.poll() is not None:
                execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=process.returncode)
                return process.returncode
            if time.monotonic() >= deadline:
                raise ValueError('Rust owned child did not reach its host gate')
            try:
                app, _ = files.read(runtime / 'rust-application-start.json')
            except (FileNotFoundError, files.PublicationPending):
                pass
            time.sleep(.01)
        original, resolved, _, _ = verify(runtime)
        application(app, original, resolved, process.pid)
        if os.getpgid(app['pid']) != app['pid'] or os.getsid(app['pid']) != app['pid']:
            raise ValueError('Rust child ownership is not observable')
        parent = subprocess.check_output(['/bin/ps', '-o', 'ppid=', '-p', str(app['pid'])],
                                         text=True, timeout=3).strip()
        if parent != str(process.pid):
            raise ValueError('Rust child is not owned by the observed supervisor')
        owned_pid = app['pid']
        release(runtime, pid=app['pid'], supervisor_pid=process.pid)
        code = process.wait()
        execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=code)
        return code
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # The supervisor normally reaps its child. If it cannot finish,
                # cancel only the already validated child's private group.
                if owned_pid is not None:
                    try:
                        if os.getpgid(owned_pid) == owned_pid:
                            os.killpg(owned_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.kill()
                process.wait(timeout=3)
