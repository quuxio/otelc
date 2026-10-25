"""Source-bound Clang semantic inventory for automatic native lifetime probes."""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from otelc import llvm_backend, native_gate, native_launch
from otelc.config import Policy
from otelc.python_sdk_io import decode

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'native/clang/Lifetimes.cpp'
PROFILE = 'clang22-cxx-lifetimes-v1'
CONTEXT_PROFILE = 'clang22-cxx-context-v1'
MAX_AST = 64 * 1024 * 1024
MANIFEST = 'native-lifetime-tool.json'


def _run(command: list[str], root: Path, output: Path, label: str) -> None:
    stdout, stderr = output / (label + '.json'), output / (label + '.stderr')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('OTEL_', 'OTELC_'))}
    with stdout.open('xb') as out, stderr.open('xb') as err:
        process = subprocess.Popen(command, cwd=root, env=environment, stdout=out, stderr=err)
        deadline = time.monotonic() + 120
        try:
            while process.poll() is None:
                if out.tell() > MAX_AST or err.tell() > 1024 * 1024 or time.monotonic() >= deadline:
                    raise ValueError('Clang lifetime query exceeded its output or time budget')
                try:
                    process.wait(timeout=.05)
                except subprocess.TimeoutExpired:
                    pass
            if stdout.stat().st_size > MAX_AST or stderr.stat().st_size > 1024 * 1024:
                raise ValueError('Clang lifetime query exceeded its output limit')
            if process.returncode:
                raise ValueError('Clang lifetime query failed; inspect ' + str(stderr))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def _dependencies(path: Path, root: Path) -> dict[str, dict[str, Any]]:
    return {row['path']: native_gate.identity(Path(row['path']))
            for row in native_launch._inputs(path, root)}


@dataclass(frozen=True)
class Tool:
    """Caller-held build identity for the fixed Clang helper and its dependencies."""

    directory: Path
    encoded: str

    def manifest(self) -> dict[str, Any]:
        """Decode a fresh copy of the caller's expected build receipt."""
        return json.loads(self.encoded)

    def verify(self) -> dict[str, Any]:
        """Refuse changed helper, compiler, input bytes or build receipt."""
        value = self.manifest()
        if ((self.directory / MANIFEST).read_text() != self.encoded
                or value['source']['path'] != str(SOURCE)
                or value['product']['path'] != str(self.directory / 'lifetimes')
                or not llvm_backend.unchanged(value['toolchain'])
                or any(native_gate.identity(Path(row['path'])) != row
                       for row in [value['source'], value['product'],
                                   *value['libraries'], *value['inputs']])):
            raise ValueError('native lifetime tool inputs changed')
        return value


def _library(directory: Path, name: str) -> Path:
    for suffix in ('.dylib', '.so'):
        path = directory / ('lib' + name + suffix)
        if path.is_file():
            return path.resolve(strict=True)
    raise ValueError('native lifetimes require the installed shared ' + name + ' library')


def build_tool(output: Path, *, llvm_config: Path | None = None,
               coverage: bool = False) -> Tool:
    """Build the fixed Clang AST helper; never compile application-supplied tooling."""
    tools = llvm_backend.discover(llvm_config)
    compiler = str(Path(tools['directory']) / 'clang++')
    config = Path(tools['tools']['llvm_config']['path'])
    library_dir = Path(llvm_backend.query(config, '--libdir'))
    libraries = [native_gate.identity(_library(library_dir, name))
                 for name in ('clang-cpp', 'LLVM')]
    source = native_gate.identity(SOURCE)
    output.mkdir(mode=0o700)
    output = output.resolve(strict=True)
    common = [compiler, *tools['cxxflags'], '-std=c++17', '-fexceptions', '-O1', '-g']
    dependency = output / 'tool.d'
    query = [*common, '-M', '-MT', 'otelc-inputs', '-MF', str(dependency), str(SOURCE)]
    _run(query, ROOT, output, 'tool-dependencies')
    inputs = _dependencies(dependency, ROOT)
    command = [*common, str(SOURCE), '-L' + str(library_dir),
               '-Wl,-rpath,' + str(library_dir), '-lclang-cpp', '-lLLVM',
               '-o', str(output / 'lifetimes')]
    if coverage:
        command.extend(['-fprofile-instr-generate', '-fcoverage-mapping'])
    _run(command, ROOT, output, 'tool-build')
    _run(query, ROOT, output, 'tool-dependencies-after')
    if (inputs != _dependencies(dependency, ROOT) or source != native_gate.identity(SOURCE)
            or not llvm_backend.unchanged(tools)
            or any(native_gate.identity(Path(row['path'])) != row for row in libraries)):
        raise ValueError('native lifetime tool inputs changed during compilation')
    value = {'schema_version': 1, 'kind': 'otelc.native.lifetime-tool',
             'toolchain': tools, 'source': source, 'libraries': libraries,
             'inputs': list(inputs.values()), 'product': native_gate.identity(output / 'lifetimes'),
             'command': command, 'coverage': coverage}
    encoded = json.dumps(value, sort_keys=True, indent=2) + '\n'
    (output / MANIFEST).write_text(encoded)
    (output / 'compile_commands.json').write_text(json.dumps([
        {'directory': str(ROOT), 'file': str(SOURCE), 'arguments': command}], indent=2) + '\n')
    return Tool(output, encoded)


def catalogue(document: dict[str, Any], policy: Policy, root: Path) -> dict[str, Any]:
    """Apply common selectors to canonical type bindings supplied by Clang."""
    if (document.get('kind') != 'otelc.clang.lifetime-semantics'
            or type(document.get('schema_version')) is not int or document['schema_version'] != 1
            or any(not isinstance(document.get(key), list)
                   for key in ('types', 'constructions', 'variables'))):
        raise ValueError('invalid Clang lifetime semantic document')
    sources, lifetimes = policy.selection('sources'), policy.selection('lifetimes')

    def selected(location: dict[str, Any]) -> bool:
        path = Path(location['file'])
        return path.is_relative_to(root) and sources.accepts(path.relative_to(root).as_posix())

    types = {}
    for row in document['types']:
        if not row['usr'] or row['usr'] in types:
            raise ValueError('Clang lifetime type identity is missing or duplicated')
        types[row['usr']] = row
    accepted = {key: row for key, row in types.items()
                if selected(row['location']) and lifetimes.accepts(row['name']) and not row['dependent']}
    result = {'types': list(accepted.values()), 'constructions': [], 'variables': [], 'unresolved': []}
    for category in ('constructions', 'variables'):
        for row in document[category]:
            if row['type_usr'] not in types:
                raise ValueError('Clang lifetime site has no canonical type binding')
            if row['type_usr'] in accepted and selected(row['location']):
                result[category].append({**row, 'type': accepted[row['type_usr']]['name']})
    return result


def compile_context(value: Any, root: Path, sources: list[Path]) -> dict[str, Any]:
    """Reuse the public unit recipe's literal flags and private-input boundaries."""
    from otelc import native_units

    if (type(value) is not dict or set(value) != {'directory', 'flags', 'overlay', 'assets'}
            or len(sources) != 1):
        raise ValueError('native lifetime context requires one unit and closed context fields')
    recipe = native_units.validate({'schema_version': 1, 'kind': native_units.RECIPE_KIND,
        'assets': value['assets'], 'units': [{'id': 'inventory', 'role': 'application',
            'source': str(sources[0]), 'language': 'cpp', 'directory': value['directory'],
            'flags': value['flags'], 'overlay': value['overlay'], 'rename_main': False}],
        'link': {'language': 'cpp', 'directory': str(root), 'flags': []}}, root)
    unit = recipe['units'][0]
    return {key: unit[key] for key in ('directory', 'flags', 'overlay')} | {'assets': recipe['assets']}


def inspect(policy: Policy, root: Path, sources: list[Path], output: Path,
            *, tool: Tool | None = None, llvm_config: Path | None = None,
            target: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inventory original C++ source without executing or rewriting the application."""
    if 'cpp' not in policy.data['languages'] or not policy.data['lifetimes']['enabled']:
        raise ValueError('native lifetime inspection requires enabled C++ lifetimes')
    root = root.resolve(strict=True)
    selected = [(root / source).resolve(strict=True) for source in sources]
    if (not root.is_dir() or not 1 <= len(selected) <= 256 or len(set(selected)) != len(selected)
            or any(not path.is_relative_to(root) or path.suffix not in ('.cpp', '.cc', '.cxx', '.C')
                   for path in selected) or output.resolve().is_relative_to(root)):
        raise ValueError('native lifetime inspection needs distinct C++ inputs and an external new output')
    context = compile_context(context, root, selected) if context is not None else None
    cwd = Path(context['directory']) if context is not None else root
    private = [native_gate.identity(Path(name)) for name in context['assets']] if context is not None else []
    output.mkdir(mode=0o700)
    output = output.resolve(strict=True)
    tool = tool or build_tool(output / 'tool', llvm_config=llvm_config)
    receipt = tool.verify()
    tools = receipt['toolchain']
    compiler = str(Path(tools['directory']) / 'clang++')
    target = target or llvm_backend.query(Path(compiler), '-print-target-triple')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', target) or not ('-apple-' in target or '-linux-' in target):
        raise ValueError('native lifetime inspection requires an Itanium macOS/Linux target')
    before = [native_gate.identity(path) for path in selected]
    records, inputs, commands = [], {}, []
    for index, source in enumerate(selected):
        prefix = str(index)
        common = [compiler, '--target=' + target, '-std=c++17', '-g', '-fexceptions', '-pthread', '-I', str(root)]
        if context is not None:
            common = [compiler, '--target=' + target, *context['flags'], '-g']
            if context['overlay'] is not None:
                common += ['-ivfsoverlay', context['overlay']]
        dep = output / (prefix + '.d')
        query = [*common, '-M', '-MT', 'otelc-inputs', '-MF', str(dep), str(source)]
        _run(query, cwd, output, prefix + '-dependencies')
        held = _dependencies(dep, cwd)
        directory = ['--compile-directory', str(cwd)] if context is not None else []
        command = [str(tool.directory / 'lifetimes'), str(root), str(source), *directory, *common]
        _run(command, cwd, output, prefix + '-semantics')
        _run(query, cwd, output, prefix + '-dependencies-after')
        if held != _dependencies(dep, cwd):
            raise ValueError('native lifetime compiler inputs changed during inspection')
        commands.extend([query, command])
        path = output / (prefix + '-semantics.json')
        raw = path.read_bytes()
        facts = catalogue(decode(raw), policy, root)
        compressed = path.with_suffix('.json.gz')
        compressed.write_bytes(gzip.compress(raw, mtime=0))
        path.unlink()
        records.append({'source': str(source), **facts, 'semantic': native_gate.identity(compressed),
                        'semantic_sha256': hashlib.sha256(raw).hexdigest(), 'semantic_bytes': len(raw)})
        inputs.update(held)
    if (before != [native_gate.identity(path) for path in selected]
            or any(native_gate.identity(Path(name)) != value for name, value in inputs.items())
            or private != ([native_gate.identity(Path(name)) for name in context['assets']]
                           if context is not None else [])):
        raise ValueError('native lifetime inputs changed before inventory publication')
    tool.verify()
    result = {'schema_version': 2 if context is not None else 1,
              'kind': 'otelc.native.lifetime-inventory', 'profile': CONTEXT_PROFILE if context is not None else PROFILE,
              'root': str(root), 'target': target, 'tool': receipt,
              'policy_sha256': native_launch.digest(policy.data), 'inputs': list(inputs.values()),
              'commands': commands, 'translation_units': records, 'source_complete': False}
    if context is not None:
        result.update(compile_context=context, context_inputs=private)
    (output / 'native-lifetimes.json').write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    return result
