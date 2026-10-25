"""Explicit LLVM 22 C/C++ launch candidate using an owned SDK shared library."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from otelc import llvm_backend
from otelc.config import Policy
from otelc.file_inputs import identity

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = r'''#include <cstdlib>
#include <cerrno>
#include <climits>
#include "otelc/native_runtime.h"
__attribute__((constructor(0))) static void otelc_bootstrap() {
  const char *raw = std::getenv("OTELC_NATIVE_PLAN_FD");
  if (!raw || !*raw) std::_Exit(78);
  char *end = nullptr; errno = 0;
  long fd = std::strtol(raw, &end, 10);
  if (errno || *end || fd < 3 || fd > INT_MAX) std::_Exit(78);
  if (otelc_native_start_v1(static_cast<int>(fd))) std::_Exit(78);
  if (std::atexit(otelc_native_finish_v1)) std::_Exit(78);
}
'''


def digest(value: object) -> str:
    """Hash a canonical translated policy or source selection."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_policy(policy: Policy, language: str) -> None:
    """Refuse pending capabilities instead of silently omitting requested probes."""
    if language not in ('c', 'cpp') or language not in policy.data['languages']:
        raise ValueError('native launch requires an enabled c or cpp language')
    if policy.data['lifetimes']['enabled']:
        raise ValueError('automatic native lifetimes are not implemented yet')
    if any(policy.data['context'].values()):
        raise ValueError('automatic native task/service propagation is not implemented yet')


@dataclass(frozen=True)
class Build:
    """Caller-held immutable expected build identity, never a manifest-selected command."""

    directory: Path
    encoded: str
    plugin: llvm_backend.Plugin

    def manifest(self) -> dict[str, Any]:
        """Decode a fresh copy of the retained build identity."""
        return json.loads(self.encoded)


def _compile(command: list[str], root: Path, output: Path, label: str,
             environment: dict[str, str], *, timeout: float = 180) -> None:
    result = subprocess.run(command, cwd=root, env=environment, capture_output=True, timeout=timeout)
    (output / f'{label}.stdout').write_bytes(result.stdout)
    (output / f'{label}.stderr').write_bytes(result.stderr)
    if result.returncode:
        raise ValueError(f'native compilation failed; see {output / (label + ".stderr")}')


def _inputs(dependencies: Path, root: Path) -> list[dict[str, Any]]:
    # -MT supplies a fixed target; shlex handles Clang's escaped spaces and lines.
    raw = dependencies.read_text().replace('\\\n', '')
    target, separator, names = raw.partition(':')
    if not separator or target != 'otelc-inputs':
        raise ValueError('invalid native dependency output')
    return [identity((root / name.replace('$$', '$')).resolve(strict=True)) for name in shlex.split(names)]


def build(policy: Policy, language: str, root: Path, sources: list[Path], output: Path,
          *, library: Path, llvm_config: Path | None = None) -> Build:
    """Compile matched plain/woven inputs; the SDK library is an explicit operator input.

    This development profile records the chosen library bytes. It does not claim
    dependency closure or that an arbitrary supplied library came from our CMake.
    """
    validate_policy(policy, language)
    root = root.resolve(strict=True)
    if not root.is_dir() or root == Path('/') or not sources or len(sources) > 256:
        raise ValueError('native build requires an application root and 1..256 translation units')
    selected = [(root / source).resolve(strict=True) for source in sources]
    suffixes = ('.c',) if language == 'c' else ('.cc', '.cpp', '.cxx', '.C')
    if len(set(selected)) != len(selected) or any(not path.is_relative_to(root) or path.suffix not in suffixes for path in selected):
        raise ValueError('native translation units must be distinct matching files inside the application root')
    before = [identity(path) for path in selected]
    library = library.resolve(strict=True)
    sdk = identity(library)
    if output.resolve().is_relative_to(root):
        raise ValueError('native build output must be outside the application root')
    output.mkdir(mode=0o700)
    output = output.resolve(strict=True)
    plugin = llvm_backend.build(output / 'plugin', config=llvm_config,
                               function_registry=True, source_aware=True)
    metadata = llvm_backend.verify(plugin)
    tools = metadata['toolchain']
    clang = str(Path(tools['directory']) / ('clang' if language == 'c' else 'clang++'))
    cxx = str(Path(tools['directory']) / 'clang++')
    environment = llvm_backend.environment(policy, language, source_root=root)
    flags = ['-O2', '-g', '-fexceptions', '-pthread', '-std=c11' if language == 'c' else '-std=c++17', '-I', str(root)]
    commands: list[list[str]] = []
    inputs: dict[str, dict[str, Any]] = {}
    products: list[dict[str, Any]] = []
    for lane in ('plain', 'instrumented'):
        objects = []
        lane_inputs: dict[str, dict[str, Any]] = {}
        for index, source in enumerate(selected):
            label = f'{lane}-{index}'
            obj, dep = output / f'{label}.o', output / f'{label}.d'
            command = [clang, *flags, '-MMD', '-MF', str(dep), '-MT', 'otelc-inputs', '-c', str(source), '-o', str(obj)]
            if lane == 'instrumented':
                command.append('-fpass-plugin=' + metadata['plugin']['path'])
            _compile(command, root, output, label, environment)
            commands.append(command)
            objects.append(str(obj))
            for row in _inputs(dep, root):
                if row['path'] in lane_inputs and row != lane_inputs[row['path']]:
                    raise ValueError('native source/header changed during compilation')
                lane_inputs[row['path']] = row
        if lane == 'plain':
            inputs = lane_inputs
        elif inputs != lane_inputs:
            raise ValueError('plain and instrumented native compilation consumed different inputs')
        if lane == 'instrumented':
            bootstrap = output / 'bootstrap.cpp'
            bootstrap.write_text(BOOTSTRAP)
            obj = output / 'bootstrap.o'
            command = [cxx, '-std=c++17', '-O2', '-g', '-c', str(bootstrap), '-o', str(obj),
                       '-I', str(ROOT / 'adapters/native/launcher/include')]
            _compile(command, root, output, 'bootstrap', environment)
            commands.append(command)
            # Mach-O preserves cross-object initializer link order; priorities
            # alone do not put this bootstrap ahead of application initializers.
            objects.insert(0, str(obj))
        binary = output / lane
        command = [cxx, '-pthread', *objects, '-o', str(binary)]
        if lane == 'instrumented':
            command.extend([str(library), '-Wl,-rpath,' + str(library.parent)])
        _compile(command, root, output, lane + '-link', environment)
        commands.append(command)
        products.append(identity(binary))
    if before != [identity(path) for path in selected] or sdk != identity(library):
        raise ValueError('native launch inputs changed while building')
    for row in inputs.values():
        if identity(Path(row['path'])) != row:
            raise ValueError('native source/header changed while building')
    llvm_backend.verify(plugin)
    value = {'schema_version': 1, 'kind': 'otelc.native-build', 'profile': 'llvm22-native-launch-v1',
             'language': language, 'root': str(root), 'policy': policy.data, 'policy_sha256': digest(policy.data),
             'selection_sha256': digest({key: policy.data[key] for key in ('sources', 'functions')}),
             'inputs': list(inputs.values()), 'sdk_library': sdk, 'products': products,
             'commands': commands, 'source_complete': False}
    encoded = json.dumps(value, sort_keys=True, indent=2) + '\n'
    (output / 'native-build.json').write_text(encoded)
    return Build(output, encoded, plugin)


def verify(value: Build) -> dict[str, Any]:
    """Recheck the exact caller-held build and fixed products before either lane."""
    if type(value) is not Build or (value.directory / 'native-build.json').read_text() != value.encoded:
        raise ValueError('native launch requires an unchanged caller-held build')
    metadata = value.manifest()
    llvm_backend.verify(value.plugin)
    for row in [*metadata['inputs'], metadata['sdk_library'], *metadata['products']]:
        if identity(Path(row['path'])) != row:
            raise ValueError('native source, SDK or executable changed after building')
    if metadata['profile'] == 'llvm22-native-units-v1':
        from otelc import native_units
        native_units.verify(value, metadata)
    return metadata


def start(value: Build, runtime: Path, *, plain: bool = False,
          arguments: tuple[str, ...] = (), **streams: Any) -> subprocess.Popen[bytes]:
    """Launch unchanged application code with an inherited private translated plan."""
    return _start(value, runtime, plain, arguments, subprocess.Popen, None, streams)


def start_waiting(value: Build, runtime: Path, gate: Any, *, policy: Policy,
                  plain: bool = False, arguments: tuple[str, ...] = (),
                  wait_ms: int = 30000, **streams: Any) -> Any:
    """Start a held native image in either lane; the host releases it separately."""
    from otelc import native_gate

    def spawn(command: list[str], *, cwd: str, env: dict[str, str],
              pass_fds: tuple[int, ...] = (), **channels: Any) -> native_gate.Waiting:
        return native_gate.start(gate, tuple(command), cwd=Path(cwd), environment=env,
                                 pass_fds=pass_fds, wait_ms=wait_ms, **channels)

    return _start(value, runtime, plain, arguments, spawn, policy, streams)


def _start(value: Build, runtime: Path, plain: bool, arguments: tuple[str, ...],
           spawn: Callable[..., Any], current: Policy | None, streams: dict[str, Any]) -> Any:
    """Share the existing fixed invocation/plan while keeping the gate explicit."""
    metadata = verify(value)
    if current is not None:
        validate_policy(current, metadata['language'])
        if digest({key: current.data[key] for key in ('sources', 'functions')}) != metadata['selection_sha256']:
            raise ValueError('native policy differs from compiled selection')
        metadata['policy'] = current.data
        metadata['policy_sha256'] = digest(current.data)
    runtime.mkdir(mode=0o700)
    runtime = runtime.resolve(strict=True)
    environment = {key: item for key, item in os.environ.items()
                   if not key.startswith(('OTEL_', 'OTELC_'))}
    command = [str(value.directory / ('plain' if plain else 'instrumented')), *arguments]
    if plain:
        return spawn(command, cwd=metadata['root'], env=environment, **streams)
    policy = metadata['policy']
    settings = dict(policy['export']) | {'service_name': policy['resource']['service_name'],
        'endpoint': policy['export']['endpoint'].rstrip('/'), **{key: policy['metrics'][key] for key in ('collection', 'emission')},
        'boundaries': policy['metrics']['histogram_boundaries_seconds'],
        'traces': policy['traces']['enabled'], 'sample_ratio': policy['traces']['sample_ratio']}
    plan = {'schema_version': 1, 'language': metadata['language'], 'runtime': str(runtime),
            'policy_sha256': metadata['policy_sha256'], 'selection_sha256': metadata['selection_sha256'],
            'settings': settings, 'token': secrets.token_hex(32)}
    path = runtime / 'plan.json'
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(json.dumps(plan).encode())
        environment['OTELC_NATIVE_PLAN_FD'] = str(fd)
        return spawn(command, cwd=metadata['root'], env=environment, pass_fds=(fd,), **streams)
    finally:
        os.close(fd)


def build_sdk(output: Path, *, coverage: bool = False) -> dict[str, Any]:
    """Build the pinned official HTTP SDK pipeline with the matched LLVM compiler."""
    tools = llvm_backend.discover()
    cmake, ninja = shutil.which('cmake'), shutil.which('ninja')
    if not cmake or not ninja:
        raise ValueError('native launch SDK requires CMake and Ninja')
    compiler = Path(tools['directory'])
    source = ROOT / 'adapters/native'
    sources = [source / 'CMakeLists.txt', source / 'launcher/CMakeLists.txt',
               *sorted(source.rglob('*.cpp')), *sorted(source.rglob('*.h')),
               ROOT / 'native/llvm/function_abi.h']
    before = [identity(path) for path in sources]
    build_tools = [identity(Path(cmake)), identity(Path(ninja))]
    output.mkdir(mode=0o700)
    output = output.resolve(strict=True)
    command = [cmake, '-S', str(source / 'launcher'), '-B', str(output), '-G', 'Ninja',
               '-DCMAKE_BUILD_TYPE=RelWithDebInfo', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
               '-DCMAKE_C_COMPILER=' + str(compiler / 'clang'),
               '-DCMAKE_CXX_COMPILER=' + str(compiler / 'clang++'),
               '-DOTELC_LAUNCH_COVERAGE=' + ('ON' if coverage else 'OFF')]
    if sys.platform == 'darwin' and Path('/opt/homebrew/opt/protobuf').is_dir():
        command.extend(['-DCMAKE_PREFIX_PATH=/opt/homebrew/opt/protobuf', '-DCURL_ROOT=/opt/homebrew/opt/curl'])
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('OTEL_', 'OTELC_'))}
    _compile(command, ROOT, output, 'configure', environment)
    command_build = [cmake, '--build', str(output), '--target', 'otelc_native_runtime', '-j', '4']
    _compile(command_build, ROOT, output, 'sdk-build', environment, timeout=900)
    library = output / ('libotelc_native_runtime.dylib' if sys.platform == 'darwin' else 'libotelc_native_runtime.so')
    if before != [identity(path) for path in sources] or not llvm_backend.unchanged(tools):
        raise ValueError('native SDK sources/compiler changed during building')
    if build_tools != [identity(Path(cmake)), identity(Path(ninja))]:
        raise ValueError('native SDK build tools changed during building')
    result = {'schema_version': 1, 'kind': 'otelc.native-sdk-build', 'sources': before,
              'toolchain': tools, 'build_tools': build_tools, 'commands': [command, command_build],
              'library': identity(library), 'coverage': coverage, 'source_complete': False}
    (output / 'native-sdk-build.json').write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    return result


def main() -> None:
    """Build the shared SDK library needed by the explicit native CLI candidate."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new SDK build directory')
    parser.add_argument('--coverage', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build_sdk(args.output, coverage=args.coverage), sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
