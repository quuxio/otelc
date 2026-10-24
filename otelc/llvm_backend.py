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
MANIFEST = 'llvm-plugin.json'
PROFILE = 'llvm22-itanium-v1'


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


def environment(policy: Policy, language: str) -> dict[str, str]:
    """Translate common function globs; compiler selection does not control metrics."""
    if language not in ('c', 'cpp') or language not in policy.data['languages']:
        raise ValueError('LLVM function component requires an enabled C/C++ language')
    values = {key: value for key, value in os.environ.items() if not key.startswith('OTELC_')}
    values.update(OTELC_FUNCTION_INCLUDE=json.dumps(policy.data['functions']['include']),
                  OTELC_FUNCTION_EXCLUDE=json.dumps(policy.data['functions']['exclude']),
                  OTELC_READ_ANNOTATIONS='0')
    if language == 'cpp':
        values['OTELC_LLVM_CPP'] = '1'
    return values


@dataclass(frozen=True)
class Plugin:
    """Caller-held build result; an edited on-disk receipt cannot replace it."""

    directory: Path
    encoded: str

    def manifest(self) -> dict[str, Any]:
        """Return a fresh decoded view without exposing mutable expected data."""
        return json.loads(self.encoded)


def build(output: Path, *, config: Path | None = None, coverage: bool = False) -> Plugin:
    """Build a fresh policy-neutral plugin and bind the actual inputs/product."""
    if type(coverage) is not bool:
        raise ValueError('LLVM coverage selection must be boolean')
    tools = discover(config)
    source = identity(SOURCE)
    output.mkdir(mode=0o700)
    output = output.resolve(strict=True)
    plugin = output / ('libotelc_llvm.dylib' if sys.platform == 'darwin' else 'libotelc_llvm.so')
    command = [str(Path(tools['directory']) / 'clang++'), '-shared', '-fPIC', '-O2',
               *tools['cxxflags'], str(SOURCE), *tools['ldflags'], '-o', str(plugin)]
    if coverage:
        command.extend(['-fprofile-instr-generate', '-fcoverage-mapping'])
    result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=180)
    (output / 'build.stdout').write_bytes(result.stdout)
    (output / 'build.stderr').write_bytes(result.stderr)
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    (output / 'compile_commands.json').write_text(json.dumps([
        {'directory': str(ROOT), 'file': str(SOURCE), 'arguments': command}], indent=2) + '\n')
    if result.returncode:
        raise ValueError('LLVM plugin compilation failed; see build.stderr')
    if identity(SOURCE) != source or not unchanged(tools):
        raise ValueError('LLVM source or tooling changed during compilation')
    value = {'schema_version': 1, 'kind': 'otelc.llvm-plugin', 'profile': PROFILE,
             'source': source, 'toolchain': tools, 'plugin': identity(plugin),
             'evidence': {name: identity(output / name) for name in
                          ('command.json', 'compile_commands.json', 'build.stdout', 'build.stderr')},
             'coverage': coverage, 'probe_abi': 'otelc.function.v1', 'source_complete': False}
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
    name = 'libotelc_llvm.dylib' if sys.platform == 'darwin' else 'libotelc_llvm.so'
    if (value['source'] != identity(SOURCE) or value['plugin'] != identity(output / name)
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
    options = parser.parse_args(arguments)
    plugin = build(options.output, config=options.llvm_config)
    print(json.dumps(verify(plugin), sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
