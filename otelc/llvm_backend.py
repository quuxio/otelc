"""Build and bind the LLVM function-hook component; native SDK launch is separate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from otelc.config import Policy
from otelc.file_inputs import checked_file

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'native/llvm/OtelcPass.cpp'
HEADER = ROOT / 'native/llvm/function_abi.h'
MANIFEST = 'llvm-plugin.json'
PROFILE = 'llvm22-itanium-v1'
REGISTERED_PROFILE = 'llvm22-itanium-registered-v1'
SOURCE_PROFILE = 'llvm22-itanium-source-v1'
OBJECT_PROFILE = 'llvm22-itanium-objects-v1'
RESOURCE_PROFILE = 'llvm22-c-resources-v1'
OBJECT_INPUTS = (ROOT / 'native/llvm/ObjectPass.cpp', ROOT / 'native/llvm/object_pass.h',
                 ROOT / 'native/llvm/object_abi.h')
RESOURCE_INPUTS = (ROOT / 'native/llvm/ResourcePass.cpp', ROOT / 'native/llvm/resource_pass.h',
                   ROOT / 'native/llvm/resource_abi.h')


def identity(path: Path) -> dict[str, Any]:
    """Hash installed tools without loading large compiler binaries into memory."""
    path = path.resolve(strict=True)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('LLVM tools and artifacts must be regular files')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    after = path.stat()
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise ValueError('LLVM tool or source changed while reading')
    return {'path': str(path), 'size': before.st_size, 'sha256': digest.hexdigest()}


def query(tool: Path, *arguments: str) -> str:
    """Execute selected operator tooling with literal argv and bounded output."""
    result = subprocess.run([str(tool), *arguments], capture_output=True,
                            text=True, timeout=60, check=True)
    if len(result.stdout) > 1024 * 1024:
        raise ValueError('LLVM tool query exceeds its output limit')
    return result.stdout.strip()


def discover(config: Path | None = None) -> dict[str, Any]:
    """Require the config and C/C++ compiler to report one exact LLVM 22 version."""
    if config is None:
        explicit = os.environ.get('LLVM_CONFIG')
        directory = os.environ.get('OTELC_LLVM_BIN')
        homebrew = Path('/opt/homebrew/opt/llvm@22/bin/llvm-config')
        selected = explicit or (str(Path(directory) / 'llvm-config') if directory else None)
        selected = selected or (str(homebrew) if homebrew.is_file() else shutil.which('llvm-config-22'))
        selected = selected or shutil.which('llvm-config')
        if not selected:
            raise ValueError('Install LLVM 22 or select its LLVM_CONFIG')
        config = Path(selected)
    config = config.resolve(strict=True)
    config_input = identity(config)
    version = query(config, '--version')
    if re.fullmatch(r'22\.\d+\.\d+', version) is None:
        raise ValueError('LLVM backend requires a stable LLVM 22 toolchain')
    directory = Path(query(config, '--bindir')).resolve(strict=True)
    tools = {'llvm_config': config_input}
    for name in ('clang', 'clang++'):
        path = directory / name
        tools[name] = identity(path)
        match = re.search(r'\bclang version (\d+\.\d+\.\d+)', query(path, '--version'))
        if match is None or match[1] != version:
            raise ValueError('LLVM compiler does not match llvm-config exactly')
    if identity(config) != config_input:
        raise ValueError('LLVM config changed during discovery')
    return {'version': version, 'directory': str(directory), 'tools': tools,
            'cxxflags': shlex.split(query(config, '--cxxflags')),
            'ldflags': shlex.split(query(config, '--ldflags', '--libs', 'core', 'passes', '--system-libs'))}


def environment(policy: Policy, language: str, *, source_root: Path | None = None) -> dict[str, str]:
    """Translate common function globs; compiler selection does not control metrics."""
    if language not in ('c', 'cpp') or language not in policy.data['languages']:
        raise ValueError('LLVM function component requires an enabled C/C++ language')
    values = {key: value for key, value in os.environ.items() if not key.startswith('OTELC_')}
    values.update(OTELC_FUNCTION_INCLUDE=json.dumps(policy.data['functions']['include']),
                  OTELC_FUNCTION_EXCLUDE=json.dumps(policy.data['functions']['exclude']),
                  OTELC_READ_ANNOTATIONS='0')
    if language == 'cpp':
        values['OTELC_LLVM_CPP'] = '1'
    if source_root is not None:
        if not source_root.is_dir() or source_root.resolve(strict=True) != source_root or source_root == Path('/'):
            raise ValueError('LLVM source profile requires a canonical application root')
        values.update(OTELC_SOURCE_ROOT=str(source_root),
                      OTELC_SOURCE_INCLUDE=json.dumps(policy.data['sources']['include']),
                      OTELC_SOURCE_EXCLUDE=json.dumps(policy.data['sources']['exclude']))
    return values


@dataclass(frozen=True)
class Plugin:
    """Caller-held build result; an edited on-disk receipt cannot replace it."""

    directory: Path
    encoded: str

    def manifest(self) -> dict[str, Any]:
        """Return a fresh decoded view without exposing mutable expected data."""
        return json.loads(self.encoded)


def build(output: Path, *, config: Path | None = None, coverage: bool = False,
          function_registry: bool = False, source_aware: bool = False,
          object_lifetimes: bool = False, resource_lifetimes: bool = False) -> Plugin:
    """Build a fresh policy-neutral plugin and bind the actual inputs/product."""
    if any(type(flag) is not bool for flag in (coverage, function_registry, source_aware, object_lifetimes, resource_lifetimes)):
        raise ValueError('LLVM coverage and function registry selection must be boolean')
    if object_lifetimes and resource_lifetimes:
        raise ValueError('LLVM C resource and C++ object profiles are separate')
    if (object_lifetimes or resource_lifetimes) and not source_aware:
        raise ValueError('LLVM object profile requires registered source-aware function instrumentation')
    if source_aware and not function_registry:

        raise ValueError('LLVM source profile requires function registration')
    tools = discover(config)
    source = identity(SOURCE)
    header = identity(HEADER)
    object_inputs = [identity(path) for path in OBJECT_INPUTS] if object_lifetimes else []
    resource_inputs = [identity(path) for path in RESOURCE_INPUTS] if resource_lifetimes else []
    output.mkdir(mode=0o700)
    output = output.resolve(strict=True)
    plugin = output / ('libotelc_llvm.dylib' if sys.platform == 'darwin' else 'libotelc_llvm.so')
    command = [str(Path(tools['directory']) / 'clang++'), '-shared', '-fPIC', '-O2',
               *tools['cxxflags'], str(SOURCE), *tools['ldflags'], '-o', str(plugin)]
    if coverage:
        command.extend(['-fprofile-instr-generate', '-fcoverage-mapping'])
    if function_registry:
        command.append('-DOTELC_FUNCTION_REGISTRY_V1=1')
    if source_aware:
        command.append('-DOTELC_SOURCE_PROFILE_V1=1')
    commands = [command]
    database = [{'directory': str(ROOT), 'file': str(SOURCE), 'arguments': command}]
    boundaries = object_lifetimes or resource_lifetimes
    if boundaries:
        inputs = OBJECT_INPUTS if object_lifetimes else RESOURCE_INPUTS
        definition = '-DOTELC_OBJECT_WITH_FUNCTIONS_V1=1' if object_lifetimes else '-DOTELC_RESOURCE_WITH_FUNCTIONS_V1=1'
        common = [str(Path(tools['directory']) / 'clang++'), '-fPIC', '-O2', *tools['cxxflags'],
                  '-DOTELC_FUNCTION_REGISTRY_V1=1', '-DOTELC_SOURCE_PROFILE_V1=1',
                  definition]
        if coverage:
            common.extend(['-fprofile-instr-generate', '-fcoverage-mapping'])
        objects = [output / 'functions.o', output / ('objects.o' if object_lifetimes else 'resources.o')]
        commands = [[*common, '-c', str(source_path), '-o', str(obj)]
                    for source_path, obj in zip((SOURCE, inputs[0]), objects, strict=True)]
        database = [{'directory': str(ROOT), 'file': str(source_path), 'arguments': argv}
                    for source_path, argv in zip((SOURCE, inputs[0]), commands, strict=True)]
        commands.append([common[0], '-shared', *map(str, objects), *tools['ldflags'],
                         *(['-fprofile-instr-generate'] if coverage else []), '-o', str(plugin)])
    stdout, stderr, exit_code = bytearray(), bytearray(), 0
    for argv in commands:
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, timeout=180)
        stdout.extend(result.stdout); stderr.extend(result.stderr)
        if result.returncode:
            exit_code = result.returncode
            break
    (output / 'build.stdout').write_bytes(stdout)
    (output / 'build.stderr').write_bytes(stderr)
    (output / 'command.json').write_text(json.dumps(commands if boundaries else command, indent=2) + '\n')
    (output / 'compile_commands.json').write_text(json.dumps(database, indent=2) + '\n')
    if exit_code:
        raise ValueError('LLVM plugin compilation failed; see build.stderr')
    if identity(SOURCE) != source or identity(HEADER) != header or not unchanged(tools):
        raise ValueError('LLVM source or tooling changed during compilation')
    if object_inputs != ([identity(path) for path in OBJECT_INPUTS] if object_lifetimes else []):
        raise ValueError('LLVM object sources changed during compilation')
    if resource_inputs != ([identity(path) for path in RESOURCE_INPUTS] if resource_lifetimes else []):
        raise ValueError('LLVM C resource sources changed during compilation')
    value = {'schema_version': 1, 'kind': 'otelc.llvm-plugin',
             'profile': RESOURCE_PROFILE if resource_lifetimes else OBJECT_PROFILE if object_lifetimes else SOURCE_PROFILE if source_aware else REGISTERED_PROFILE if function_registry else PROFILE,
             'source': source, 'headers': [header], 'toolchain': tools, 'plugin': identity(plugin),
             'evidence': {name: identity(output / name) for name in
                          ('command.json', 'compile_commands.json', 'build.stdout', 'build.stderr')},
             'coverage': coverage, 'probe_abi': 'otelc.function.v2' if function_registry else 'otelc.function.v1',
             'source_complete': False}
    if object_lifetimes:
        value.update(object_inputs=object_inputs, object_probe_abi='otelc.object.v1')
    if resource_lifetimes:
        value.update(resource_inputs=resource_inputs, resource_probe_abi='otelc.resource.v1')
    encoded = json.dumps(value, sort_keys=True, indent=2) + '\n'
    (output / MANIFEST).write_text(encoded)
    return Plugin(output, encoded)


def unchanged(tools: dict[str, Any]) -> bool:
    """Recheck selected compiler aliases as well as their canonical targets."""
    return all(identity(Path(row['path']) if name == 'llvm_config' else Path(tools['directory']) / name) == row
               for name, row in tools['tools'].items())


def verify(plugin: Plugin) -> dict[str, Any]:
    """Verify with caller-selected tools; never execute a metadata-selected command."""
    if type(plugin) is not Plugin:
        raise ValueError('LLVM verification needs the caller-held build result')
    output = plugin.directory
    _, raw = checked_file(output / MANIFEST, include_content=True, nofollow=True, max_bytes=1024 * 1024)
    if raw != plugin.encoded.encode():
        raise ValueError('LLVM plugin manifest changed after compilation')
    value = plugin.manifest()
    tools = value['toolchain']
    expected_objects = [identity(path) for path in OBJECT_INPUTS] if value['profile'] == OBJECT_PROFILE else []
    if value.get('object_inputs', []) != expected_objects:
        raise ValueError('LLVM object source inputs changed')
    expected_resources = [identity(path) for path in RESOURCE_INPUTS] if value['profile'] == RESOURCE_PROFILE else []
    if value.get('resource_inputs', []) != expected_resources:
        raise ValueError('LLVM C resource source inputs changed')
    name = 'libotelc_llvm.dylib' if sys.platform == 'darwin' else 'libotelc_llvm.so'
    if (value['source'] != identity(SOURCE) or value['headers'] != [identity(HEADER)]
            or value['plugin'] != identity(output / name)
            or any(identity(output / name) != expected for name, expected in value['evidence'].items())
            or not unchanged(tools)):
        raise ValueError('LLVM plugin differs from the selected source/toolchain/product')
    return value


def main(arguments: list[str] | None = None) -> None:
    """Build the compiler component and print its verified input/product record."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True,
                        help='new output directory; existing paths are refused')
    parser.add_argument('--llvm-config', type=Path,
                        help='installed LLVM 22 llvm-config executable')
    parser.add_argument('--function-registry', action='store_true',
                        help='explicit ABI2 profile: register selected function addresses and names before initialisers')
    parser.add_argument('--source-aware', action='store_true',
                        help='explicit source profile: require -g, source selections and stable source-qualified identities')
    options = parser.parse_args(arguments)
    plugin = build(options.output, config=options.llvm_config,
                   function_registry=options.function_registry or options.source_aware, source_aware=options.source_aware)
    print(json.dumps(verify(plugin), sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
