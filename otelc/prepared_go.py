"""Fixed compiler-free Go launch recipes over separately retained source builds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import sys
from typing import Any

from otelc import file_inputs, go_build, go_build_tags, go_health, go_runtime_config, go_source, health
from otelc import python_launch_files as files
from otelc import python_recipe
from otelc.config import Policy
from otelc.python_handoff import unique_fields

ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'go-source-v1'
REQUEST, PLAN, PREPARED = 'go-request.json', 'go-plan.json', 'go-prepared.json'
BUILD_FIELDS = {'schema_version', 'kind', 'root', 'target', 'selection', 'inputs', 'sdk_inputs',
                'private_inputs', 'context_inputs', 'inventory', 'plain', 'instrumented',
                'modules_before', 'modules_after', 'settings', 'toolchain', 'commands', 'source_complete',
                'launch_inputs', 'source_roster', 'runtime_protocols'}
REQUEST_FIELDS = {'schema_version', 'kind', 'profile', 'build', 'runtime', 'policy', 'arguments', 'plain', 'launch_id'}


def protocols(*, build_tags: bool = False, prepared_tools: bool = False) -> dict[str, Any]:
    """Describe formats without compiling or claiming provider qualification."""
    value = {'schema_version': 1, 'kind': 'otelc.go.protocols', 'profile': PROFILE,
            'build': 4, 'config': 2, 'request': 1, 'plan': 1, 'prepared': 1, 'command': 1,
            'execution': 1, 'health': 4, 'embedded_sdk': True, 'source_complete': False}
    if build_tags:
        value.update(build=5, inspection=2)
    if prepared_tools:
        value['prepared_tools'] = 1
    return value


def digest(value: dict[str, Any]) -> str:
    """Use the existing canonical launch-metadata encoding for complete records."""
    return hashlib.sha256(python_recipe.encoded(value).encode()).hexdigest()


def same(value: Any, expected: Any) -> bool:
    """Canonical JSON equality distinguishes true,1 and1.0 in protocol records."""
    return python_recipe.encoded(value) == python_recipe.encoded(expected)


def read(path: Path) -> dict[str, Any]:
    """Read owned launch metadata through the existing bounded snapshot reader."""
    try:
        value, _ = files.read(path)
        return value
    except ValueError as error:
        raise ValueError('invalid Go private launch metadata: ' + path.name) from error


def directory(path: Path) -> Path:
    """Check the owned private leaf; never alter an existing directory's mode."""
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700:
        raise ValueError('Go runtime directory must be owned and mode0700')
    return path.resolve(strict=True)


def ambient(environment: dict[str, str]) -> None:
    """Refuse uncontrolled SDK settings before any application initialiser runs."""
    if any(name.startswith('OTEL_') or name in ('OTELC_GO_RUNTIME', 'OTELC_GO_CONFIG_SHA256')
           for name in environment):
        raise ValueError('Go prepared launches require unset ambient SDK and Go bootstrap variables')


def verify_build(directory: Path, policy: Policy) -> dict[str, Any]:
    """Recheck fixed products and original/private inputs without invoking Go."""
    directory = directory.resolve(strict=True)
    identity, raw = file_inputs.checked_file(directory / 'build.json', include_content=True,
                                            nofollow=True, max_bytes=go_source.MAX_INVENTORY)
    from otelc.python_sdk_io import decode
    value = decode(raw)
    tagged = value.get('schema_version') == 5
    fields = BUILD_FIELDS | {'build_tags', 'command_evidence'} if tagged else BUILD_FIELDS
    if (set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] not in (4, 5) or value['kind'] != 'otelc.go-source-build'
            or not same(value['runtime_protocols'], {'config': 2, 'health': 4})
            or value['source_complete'] is not False):
        raise ValueError('Go prepared profile requires build schema4/5 with config2/health4')
    tags = go_build_tags.record(value['build_tags']) if tagged else ()
    if tagged:
        verify_tagged_commands(directory, value, tags)
    root = Path(value['root'])
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError('Go build original root is not canonical')
    if (go_build.source_roster(root) != value['source_roster']
            or any(not same(go_source.identity(root / name), expected) for name, expected in value['launch_inputs'].items())):
        raise ValueError('Go original compiler input changed')
    for folder, field in ((directory / 'private', 'private_inputs'),
                          (directory / 'sdk', 'sdk_inputs'), (directory / 'context', 'context_inputs')):
        if not same(go_build.inputs(folder), value[field]):
            raise ValueError('Go build input changed: ' + field)
    for name in ('plain', 'instrumented'):
        if not same(go_source.identity(directory / name), value[name]):
            raise ValueError('Go build binary changed: ' + name)
    inventory_path = directory / 'inspection/inventory.json'
    if not same(go_source.identity(inventory_path), value['inventory']):
        raise ValueError('Go compiler-selected source inventory changed')
    _, inventory_bytes = file_inputs.checked_file(inventory_path, include_content=True,
                                                  nofollow=True, max_bytes=go_source.MAX_INVENTORY)
    inventory = decode(inventory_bytes)
    if (type(inventory.get('schema_version')) is not int
            or inventory['schema_version'] != (2 if tagged else 1)
            or (tagged and not same(inventory.get('build_tags'), list(tags)))
            or (not tagged and 'build_tags' in inventory)):
        raise ValueError('Go source inventory differs from the fixed build tags')
    if inventory['root'] != str(root) or inventory['target'] != value['target']:
        raise ValueError('Go source inventory differs from the fixed build recipe')
    if not same(go_source.resolve(policy, inventory), value['selection']):
        raise ValueError('Go policy differs from the compiled source/context selection')
    host = {'GOOS': platform.system().lower(),
            'GOARCH': {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine(), platform.machine())}
    if value['toolchain']['platform'] != host:
        raise ValueError('Go build is not for the current native host')
    return {'identity': identity, 'receipt': value}


def verify_tagged_commands(directory: Path, value: dict[str, Any], tags: tuple[str, ...]) -> None:
    """Check retained compiler recipes and logs without executing recorded argv."""
    commands = value['commands']
    if (type(commands) is not list or not 2 <= len(commands) <= 128
            or any(type(row) is not dict or set(row) != {'argv', 'cwd', 'exit_code'}
                   or type(row['argv']) is not list or not row['argv']
                   or any(type(arg) is not str for arg in row['argv'])
                   or type(row['cwd']) is not str or not same(row['exit_code'], 0)
                   for row in commands)):
        raise ValueError('invalid Go tagged compiler command records')
    names = {'commands.json', *[f'command-{index}.{suffix}' for index in range(len(commands))
                               for suffix in ('stdout', 'stderr')]}
    evidence = value['command_evidence']
    if (type(evidence) is not dict or set(evidence) != names
            or any(not same(go_source.identity(directory / name), evidence[name]) for name in names)):
        raise ValueError('Go tagged compiler command evidence changed')
    command_identity, raw = file_inputs.checked_file(directory / 'commands.json', include_content=True,
                                                     nofollow=True, max_bytes=go_source.MAX_INVENTORY)
    if (not same(command_identity, evidence['commands.json'])
            or not same(json.loads(raw, object_pairs_hook=unique_fields), commands)):
        raise ValueError('Go tagged compiler command evidence changed')
    flags = ['-mod=readonly', *go_build_tags.arguments(tags)]
    expected = [
        {'argv': [value['toolchain']['go'], 'build', *flags, '-o', str(directory / 'plain'), value['target']],
         'cwd': value['root'], 'exit_code': 0},
        {'argv': [value['toolchain']['weaver'], '--work-dir', str(directory / 'weaver-work'),
                  'go', 'build', *flags, '-o', str(directory / 'instrumented'), value['target']],
         'cwd': str(directory / 'private'), 'exit_code': 0}]
    if not same(commands[-2:], expected):
        raise ValueError('Go tagged compiler command differs from the fixed build recipe')


def bindings(value: dict[str, Any]) -> dict[str, Any]:
    """Keep generated function/type indices identical to the compiled hook IDs."""
    selection = value['selection']
    released = {row['lifetime'] for row in selection['releases']}
    return {'functions': tuple(row['name'] for row in selection['functions']),
            'lifetimes': tuple((row['name'], index in released) for index, row in enumerate(selection['types'])),
            'selection_sha256': selection['sha256']}


def request(policy: Policy, runtime: Path, built: dict[str, Any], arguments: tuple[str, ...],
            plain: bool, launch_id: str) -> dict[str, Any]:
    """Application arguments are bounded literals; they never select an executable."""
    if (type(plain) is not bool or type(arguments) is not tuple or len(arguments) > 4096
            or any(type(arg) is not str or '\0' in arg for arg in arguments)
            or sum(len(os.fsencode(arg)) for arg in arguments) > 1024 * 1024
            or type(launch_id) is not str or re.fullmatch('[0-9a-f]{32}', launch_id) is None):
        raise ValueError('invalid Go application arguments or comparison lane')
    return {'schema_version': 1, 'kind': 'otelc.go.request', 'profile': PROFILE,
            'build': built['identity'], 'runtime': str(runtime), 'policy': policy.data,
            'arguments': list(arguments), 'plain': plain, 'launch_id': launch_id}


def plan(original: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    """Derive exactly one stock/woven binary command from the fixed output names."""
    name = 'plain' if original['plain'] else 'instrumented'
    binary = Path(original['build']['path']).parent / name
    return {'schema_version': 1, 'kind': 'otelc.go.plan', 'profile': PROFILE,
            'request_sha256': digest(original), 'cwd': receipt['root'],
            'argv': [str(binary), *original['arguments']], 'binary': receipt[name],
            'selection_sha256': receipt['selection']['sha256'], 'source_complete': False}


def manifest(original: dict[str, Any], resolved: dict[str, Any], config: dict[str, Any] | None) -> dict[str, Any]:
    """Bind the fixed supervisor and producer files, never a command from metadata."""
    return {'schema_version': 1, 'kind': 'otelc.go.prepared', 'profile': PROFILE,
            'request_sha256': digest(original), 'plan_sha256': digest(resolved), 'config': config,
            'interpreter': file_inputs.identity(Path(sys.executable)),
            'producer': [file_inputs.identity(path) for path in sorted((ROOT / 'otelc').glob('*.py'))],
            'source_complete': False}


def prepare(policy: Policy, runtime: Path | str, *, build_directory: Path | str,
            arguments: tuple[str, ...] = (), plain: bool = False) -> dict[str, Any]:
    """Prepare a new single-use runtime without compiling or starting a process."""
    ambient(dict(os.environ))
    built = verify_build(Path(build_directory), policy)
    runtime = Path(runtime).resolve()
    excluded = [Path(built['receipt']['root']), *[Path(build_directory).resolve() / name
                for name in ('private', 'sdk', 'context', 'inspection')]]
    if any(runtime.is_relative_to(root) for root in excluded):
        raise ValueError('Go runtime must be outside checked source/build input trees')
    original = request(policy, runtime, built, arguments, plain, secrets.token_hex(16))
    config = None
    if plain:
        runtime.mkdir(mode=0o700)
        runtime.chmod(0o700)
    else:
        config = go_runtime_config.prepare(policy, runtime, **bindings(built['receipt']))
    resolved = plan(original, built['receipt'])
    prepared = manifest(original, resolved, config)
    for name, value in ((REQUEST, original), (PLAN, resolved), (PREPARED, prepared)):
        files.write(runtime / name, value)
    return {'schema_version': 1, 'kind': 'otelc.go.command', 'profile': PROFILE,
            'request': original, 'plan': resolved, 'prepared': prepared,
            'command': invocation(runtime), 'source_complete': False}


def invocation(runtime: Path) -> list[str]:
    """The caller must own a new session for this exact supervisor invocation."""
    return [sys.executable, '-I', '-S', '-B', str(ROOT / 'otelc/go_supervisor.py'),
            '--runtime', str(runtime.resolve(strict=True))]


def verify(runtime: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Rederive all prepared state without a compiler or arbitrary subprocess."""
    ambient(dict(os.environ))
    runtime = directory(runtime)
    original, resolved, prepared = (read(runtime / name) for name in (REQUEST, PLAN, PREPARED))
    if (set(original) != REQUEST_FIELDS or type(original['schema_version']) is not int
            or original['schema_version'] != 1 or original['kind'] != 'otelc.go.request'
            or original['profile'] != PROFILE or type(original['arguments']) is not list
            or type(original['policy']) is not dict or type(original['runtime']) is not str
            or type(original['build']) is not dict or type(original['build'].get('path')) is not str):
        raise ValueError('invalid Go prepared request')
    policy = Policy.parse(original['policy'])
    built = verify_build(Path(original['build']['path']).parent, policy)
    if not same(built['identity'], original['build']):
        raise ValueError('Go build receipt changed after preparation')
    expected = request(policy, runtime, built, tuple(original['arguments']), original['plain'], original['launch_id'])
    expected_plan = plan(expected, built['receipt'])
    config = None
    if not original['plain']:
        value = go_runtime_config.configuration(policy, **bindings(built['receipt']))
        identity, raw = file_inputs.checked_file(runtime / 'config.json', include_content=True, nofollow=True,
                                                max_bytes=go_runtime_config.MAX_CONFIG_BYTES)
        from otelc.python_sdk_io import decode
        if not same(decode(raw), value):
            raise ValueError('Go private runtime configuration differs from the fixed policy')
        config = {'schema_version': 1, 'kind': 'otelc.go-runtime-config', 'directory': str(runtime),
                  'config': identity, 'health_version': 4,
                  'environment': {'OTELC_GO_RUNTIME': str(runtime), 'OTELC_GO_CONFIG_SHA256': identity['sha256']}}
    if not same(original, expected) or not same(resolved, expected_plan) or not same(prepared, manifest(expected, expected_plan, config)):
        raise ValueError('Go prepared files differ from the fixed recipe')
    return original, resolved, prepared


def command(runtime: Path | str) -> list[str]:
    """Return only the rechecked fixed supervisor, never recorded argv."""
    runtime = Path(runtime)
    verify(runtime)
    if any((runtime / name).exists() for name in ('go-supervisor-start.json', 'go-application-start.json',
                                                'go-execution.json', 'health.json', 'control.json')):
        raise ValueError('Go prepared runtime is single-use and already has execution records')
    return invocation(runtime)


def execution_shape(result: dict[str, Any]) -> None:
    """Keep incomplete outcomes explicit rather than accepting coercible counters."""
    fields = {'schema_version', 'kind', 'profile', 'launch_id', 'supervisor', 'application_pid',
              'sdk_pid', 'application_exit_code', 'interrupted_by', 'inputs_rechecked', 'sdk_health',
              'failures', 'source_complete', 'downstream_receipt'}
    if (set(result) != fields or type(result['schema_version']) is not int or result['schema_version'] != 1
            or result['kind'] != 'otelc.go.execution' or result['profile'] != PROFILE
            or type(result['application_pid']) is not int or result['application_pid'] <= 1
            or type(result['application_exit_code']) is not int
            or not -128 <= result['application_exit_code'] <= 255 or type(result['inputs_rechecked']) is not bool
            or result['source_complete'] is not False or result['downstream_receipt'] != 'unavailable'
            or type(result['failures']) is not dict
            or any(type(key) is not str or type(value) is not str for key, value in result['failures'].items())
            or result['interrupted_by'] is not None and (type(result['interrupted_by']) is not int
                                                        or result['interrupted_by'] not in (2, 15))):
        raise ValueError('invalid Go execution metadata')


def sdk_evidence(runtime: Path, original: dict, resolved: dict, result: dict) -> None:
    """Require the exact final SDK report while preserving explicit unavailable data."""
    sdk_pid = None if original['plain'] else result['application_pid']
    if not same(result['sdk_pid'], sdk_pid):
        raise ValueError('Go embedded SDK identity differs from its application')
    sdk = result['sdk_health']
    if original['plain']:
        if sdk is not None or (runtime / 'health.json').exists():
            raise ValueError('plain Go execution invented SDK health')
    elif sdk is None:
        if not result['failures']:
            raise ValueError('Go execution lacks final SDK evidence without recording failure')
    else:
        report = health.read(runtime / 'health.json')
        settings = read(runtime / 'config.json')
        if (not same(sdk, report) or report['phase'] != 'final' or report['pid'] != sdk_pid
                or report['policy_sha256'] != settings['policy_sha256']
                or report['selection_sha256'] != resolved['selection_sha256']
                or not go_health.final_clean(report) and not result['failures']):
            raise ValueError('Go final SDK report does not match the execution evidence')


def supervisor_exit(code: int | None, stopped: int | None, failures: dict) -> int:
    """Retain application failures before an instrumentation or interruption status."""
    if code:
        return code if code > 0 else 128 - code
    if stopped is not None:
        return 128 + stopped
    return int(bool(failures))


def execution(runtime: Path | str, *, supervisor_pid: int, parent_pid: int,
              returncode: int) -> dict[str, Any]:
    """Join caller-observed ownership to unchanged recipes and final SDK evidence.

    The caller supplies identities from its own Popen, not from these records.
    A returned failure record remains incomplete evidence. No target is started
    and no live flush, Collector query or lifetime drain is performed here.
    """
    if (type(supervisor_pid) is not int or supervisor_pid <= 1
            or type(parent_pid) is not int or parent_pid < 1 or parent_pid == supervisor_pid
            or type(returncode) is not int or not -128 <= returncode <= 255):
        raise ValueError('Go execution requires caller-observed process and exit identities')
    runtime = Path(runtime)
    original, resolved, prepared = verify(runtime)
    owner = {'schema_version': 1, 'kind': 'otelc.go.supervisor', 'profile': PROFILE,
             'supervisor_pid': supervisor_pid, 'parent_pid': parent_pid, 'process_group': supervisor_pid,
             'launch_id': original['launch_id'], 'request_sha256': digest(original),
             'plan_sha256': digest(resolved), 'prepared_sha256': digest(prepared)}
    if not same(read(runtime / 'go-supervisor-start.json'), owner):
        raise ValueError('Go supervisor differs from caller-owned process identities')
    result = read(runtime / 'go-execution.json')
    execution_shape(result)
    if (result['launch_id'] != original['launch_id'] or not same(result['supervisor'], owner)
            or result['application_pid'] in (supervisor_pid, parent_pid)):
        raise ValueError('Go execution has a different owner or application identity')
    application = {'schema_version': 1, 'kind': 'otelc.go.application', 'profile': PROFILE,
                   'launch_id': original['launch_id'], 'pid': result['application_pid'],
                   'supervisor_pid': supervisor_pid, 'process_group': supervisor_pid,
                   'plain': original['plain'], 'argv': resolved['argv'], 'binary': resolved['binary']}
    if not same(read(runtime / 'go-application-start.json'), application):
        raise ValueError('Go application differs from the actual owned launch')
    sdk_evidence(runtime, original, resolved, result)
    if (supervisor_exit(result['application_exit_code'], result['interrupted_by'], result['failures']) != returncode
            or not result['inputs_rechecked'] and not result['failures']):
        raise ValueError('Go observed supervisor exit or input recheck differs')
    return result


def run(policy: Policy, runtime: Path | str, *, build_directory: Path | str,
        arguments: tuple[str, ...] = (), plain: bool = False, prepare_only: bool = False) -> int:
    """Own the supervisor session through application completion and cleanup."""
    prepared = prepare(policy, runtime, build_directory=build_directory, arguments=arguments, plain=plain)
    if prepare_only:
        print(json.dumps(prepared, indent=2))
        return 0
    from otelc.prepared_python import stop_group
    process = subprocess.Popen(command(runtime), start_new_session=True)
    try:
        return process.wait()
    finally:
        stop_group(process)
