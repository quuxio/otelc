"""Direct Rust candidate launch using the existing native OpenTelemetry SDK."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
from typing import Any, Callable

from otelc import health, native_gate, native_launch, rust_build, rust_compiler
from otelc.config import Policy
from otelc import python_launch_files as files


def start(built: rust_build.Build, policy: Policy, runtime: Path, *, plain: bool = False,
          arguments: tuple[str, ...] = (), **streams: Any) -> subprocess.Popen[bytes]:
    """Run either fixed executable with literal arguments and independent live gates."""
    return _start(built, policy, runtime, plain, arguments, subprocess.Popen, streams)


def start_waiting(built: rust_build.Build, policy: Policy, runtime: Path, gate: native_gate.Product, *,
                  plain: bool = False, arguments: tuple[str, ...] = (),
                  wait_ms: int = 30000, **streams: Any) -> native_gate.Waiting:
    """Start the fixed pre-exec gate; the host must release the image separately."""
    if type(wait_ms) is not int or not 1 <= wait_ms <= 300000:
        raise ValueError('invalid Rust gate wait budget')

    def spawn(command: list[str], *, cwd: str, env: dict[str, str],
              pass_fds: tuple[int, ...] = (), **channels: Any) -> native_gate.Waiting:
        return native_gate.start(gate, tuple(command), cwd=Path(cwd), environment=env,
                                 pass_fds=pass_fds, wait_ms=wait_ms, **channels)

    return _start(built, policy, runtime, plain, arguments, spawn, streams)


def _start(built: rust_build.Build, policy: Policy, runtime: Path, plain: bool,
           arguments: tuple[str, ...], spawn: Callable[..., Any], streams: dict[str, Any]) -> Any:
    """Share both fixed invocations and translated SDK plans with the direct API."""
    metadata = rust_build.verify(built, policy)
    if (type(plain) is not bool or type(arguments) is not tuple or len(arguments) > 4096
            or any(type(arg) is not str or '\0' in arg for arg in arguments)
            or sum(len(os.fsencode(arg)) for arg in arguments) > 1024 * 1024):
        raise ValueError('invalid Rust application arguments or lane')
    runtime = runtime.resolve()
    if (rust_compiler.physical_within(runtime, Path(metadata['root']))
            or rust_compiler.physical_within(runtime, built.directory)):
        raise ValueError('Rust runtime must be outside original sources and retained build')
    runtime.mkdir(mode=0o700)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('OTEL_', 'OTELC_'))}
    command = [str(built.directory / ('plain' if plain else 'instrumented')), *arguments]
    if plain:
        return spawn(command, cwd=metadata['root'], env=environment, **streams)
    data = policy.data
    settings = dict(data['export']) | {'service_name': data['resource']['service_name'],
        'endpoint': data['export']['endpoint'].rstrip('/'),
        **{key: data['metrics'][key] for key in ('collection', 'emission')},
        'boundaries': data['metrics']['histogram_boundaries_seconds'],
        'traces': data['traces']['enabled'], 'sample_ratio': data['traces']['sample_ratio']}
    if data['lifetimes']['enabled']:
        settings['drop_lifetimes'] = {'enabled': True, 'max_live': data['lifetimes']['max_live']}
    plan = {'schema_version': 3 if data['lifetimes']['enabled'] else 2, 'language': 'rust', 'runtime': str(runtime),
            'policy_sha256': native_launch.digest(data), 'selection_sha256': metadata['selection_sha256'],
            'settings': settings, 'token': secrets.token_hex(32)}
    descriptor = os.open(runtime / 'plan.json', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'wb', closefd=False) as stream:
            stream.write(json.dumps(plan).encode())
        environment['OTELC_NATIVE_PLAN_FD'] = str(descriptor)
        return spawn(command, cwd=metadata['root'], env=environment, pass_fds=(descriptor,), **streams)
    finally:
        os.close(descriptor)


def validate_health(value: dict[str, Any], built: rust_build.Build, policy: Policy, pid: int) -> None:
    """Bind the final SDK checkpoint without treating API counters as delivery proof."""
    health.validate(value)
    rust_build.validate_policy(policy)
    lifetimes = policy.data['lifetimes']['enabled']
    if (value['schema_version'] != (5 if lifetimes else 1)
            or ('drop_accounting' in value) != lifetimes):
        raise ValueError('public Rust health profile differs from the lifetime policy')
    metadata = built.manifest()
    if (value['pid'] != pid or value['language'] != 'rust' or value['phase'] != 'final'
            or value['sdk_attached'] is not True
            or value['policy_sha256'] != native_launch.digest(policy.data)
            or value['selection_sha256'] != metadata['selection_sha256']):
        raise ValueError('Rust SDK checkpoint does not identify the launched build/policy/process')


def run(policy: Policy, runtime: Path, *, build_directory: Path,
        plain: bool = False, arguments: tuple[str, ...] = ()) -> int:
    """Expose direct application execution; prepared host ownership remains separate."""
    built = rust_build.read(build_directory, policy)
    process = start(built, policy, runtime, plain=plain, arguments=arguments)
    try:
        code = process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    sdk_health = None
    failure = None
    try:
        rust_build.verify(built, policy)
        if not plain:
            sdk_health = health.read(runtime / 'health.json')
            validate_health(sdk_health, built, policy, process.pid)
    except (ValueError, OSError) as error:
        failure = str(error)
    exit_code = 128 - code if code < 0 else code
    if not exit_code and failure is not None:
        exit_code = 1
    files.write(runtime / 'rust-execution.json', {'schema_version': 1,
        'kind': 'otelc.rust.direct-execution', 'profile': rust_build.profile(policy),
        'pid': process.pid, 'process_exit_code': code, 'launcher_exit_code': exit_code,
        'sdk_health': sdk_health, 'validation_failure': failure,
        'scope': 'Direct child only; not a prepared host-release or OS ownership qualification',
        'source_complete': False})
    return exit_code
