"""Bind Clang-resolved C record allocations to the shared lifetime policy."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from otelc import native_gate, native_launch, native_lifetimes, native_units
from otelc.config import Policy
from otelc.python_sdk_io import decode

PROFILE = native_lifetimes.RESOURCE_PROFILE
KIND = 'otelc.native.c-resource-inventory'
MANIFEST = 'native-resources.json'


def require(condition: bool, reason: str) -> None:
    """Refuse incomplete C semantics before producing a compiler probe plan."""
    if not condition:
        raise ValueError('native C resources: ' + reason)


def outside(path: Path, root: Path) -> bool:
    """Protect the actual source directory, including APFS case aliases."""
    path = path.resolve()
    if path.is_relative_to(root):
        return False
    for parent in (path, *path.parents):
        try:
            if parent.samefile(root):
                return False
        except FileNotFoundError:
            continue
    return True


def catalogue(document: dict[str, Any], policy: Policy, root: Path) -> dict[str, Any]:
    """Apply type/source selection to actual Clang allocation and release sites."""
    require(type(document) is dict
            and set(document) == {'schema_version', 'kind', 'types', 'allocations', 'releases', 'unsupported'}
            and type(document['schema_version']) is int and document['schema_version'] == 1
            and document['kind'] == 'otelc.clang.c-resource-semantics'
            and all(type(document[key]) is list for key in ('types', 'allocations', 'releases', 'unsupported')),
            'invalid semantic document')
    sources, lifetimes = policy.selection('sources'), policy.selection('lifetimes')

    def selected(row: dict[str, Any]) -> bool:
        filename = Path(row['location']['file'])
        return filename.is_relative_to(root) and sources.accepts(filename.relative_to(root).as_posix())

    types = {}
    for row in document['types']:
        require(type(row) is dict and set(row) == {'usr', 'name', 'location', 'complete'}
                and type(row['usr']) is str and 0 < len(row['usr']) <= 4096 and row['usr'] not in types
                and type(row['name']) is str and len(row['name']) <= 4096
                and type(row['complete']) is bool, 'invalid or duplicate C type binding')
        types[row['usr']] = row
    accepted = {key: row for key, row in types.items() if selected(row) and lifetimes.accepts(row['name'])}
    require(len(accepted) <= 4096, 'selected type budget exceeded')
    result = {'types': list(accepted.values()), 'allocations': [], 'releases': []}
    for row in document['unsupported']:
        if (selected(row) and not row['observer']
                and ('type_usr' not in row or row['type_usr'] in accepted)):
            raise ValueError('native C resources: ' + row['reason'])
    for row in document['allocations']:
        require(row['type_usr'] in types, 'allocation has no canonical type binding')
        if row['type_usr'] not in accepted or not selected(row) or row['observer']:
            continue
        record = accepted[row['type_usr']]
        require(record['complete'] and bool(record['name']), 'selected allocation needs a complete named C record')
        require(row['callee'] in ('malloc', 'calloc') and row['standard'] is True
                and row['arguments'] == (1 if row['callee'] == 'malloc' else 2),
                'custom or incompatible allocator needs a separate profile')
        result['allocations'].append({**row, 'type': record['name']})
    for row in document['releases']:
        if selected(row) and not row['observer']:
            require(row['callee'] == 'free' and row['standard'] is True and row['arguments'] == 1,
                    'custom or incompatible free needs a separate profile')
            result['releases'].append(row)
    return result


def compile_context(root: Path, source: Path, value: dict[str, Any] | None) -> dict[str, Any]:
    """Reuse the native unit recipe's flags, private assets and VFS checks."""
    if value is None:
        value = {'directory': str(root), 'flags': ['-std=c11', '-O2', '-fexceptions', '-pthread', '-I', str(root)],
                 'overlay': None, 'assets': []}
    require(type(value) is dict and set(value) == {'directory', 'flags', 'overlay', 'assets'},
            'one closed C compilation context is required')
    recipe = native_units.validate({'schema_version': 1, 'kind': native_units.RECIPE_KIND,
        'assets': value['assets'], 'units': [{'id': 'inventory', 'role': 'application',
            'source': str(source), 'language': 'c', 'directory': value['directory'],
            'flags': value['flags'], 'overlay': value['overlay'], 'rename_main': False}],
        'link': {'language': 'c', 'directory': str(root), 'flags': []}}, root)
    return {key: recipe['units'][0][key] for key in ('directory', 'flags', 'overlay')} | {'assets': recipe['assets']}


def inspect(policy: Policy, root: Path, source: Path, output: Path, *,
            tool: native_lifetimes.Tool | None = None,
            context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inspect one original C unit; do not execute initialisers or rewrite source."""
    require('c' in policy.data['languages'] and policy.data['lifetimes']['enabled'],
            'enabled C lifetimes are required')
    root = root.resolve(strict=True)
    source = (root / source).resolve(strict=True)
    require(root.is_dir() and source.is_relative_to(root) and source.suffix == '.c' and outside(output, root),
            'a C source and new output outside the source root are required')
    context = compile_context(root, source, context)
    require(tool is None or tool.profile == PROFILE, 'the fixed C resource helper is required')
    before = native_gate.identity(source)
    policy_sha = native_launch.digest(policy.data)
    private = [native_gate.identity(Path(name)) for name in context['assets']]
    output.mkdir(mode=0o700)
    output = output.resolve(strict=True)
    tool = tool or native_lifetimes.build_tool(output / 'tool', profile=PROFILE)
    receipt = tool.verify()
    compiler = str(Path(receipt['toolchain']['directory']) / 'clang')
    common = [compiler, *context['flags'], '-g']
    if context['overlay'] is not None:
        common += ['-ivfsoverlay', context['overlay']]
    cwd = Path(context['directory'])
    dependency = output / 'source.d'
    query = [*common, '-M', '-MT', 'otelc-inputs', '-MF', str(dependency), str(source)]
    native_lifetimes._run(query, cwd, output, 'dependencies')
    held = native_lifetimes._dependencies(dependency, cwd)
    command = [str(tool.directory / 'lifetimes'), str(root), str(source),
               '--compile-directory', str(cwd), *common]
    native_lifetimes._run(command, cwd, output, 'semantics')
    native_lifetimes._run(query, cwd, output, 'dependencies-after')
    require(held == native_lifetimes._dependencies(dependency, cwd), 'compiler inputs changed during inspection')
    raw = (output / 'semantics.json').read_bytes()
    facts = catalogue(decode(raw), policy, root)
    compressed = output / 'semantics.json.gz'
    compressed.write_bytes(gzip.compress(raw, mtime=0))
    (output / 'semantics.json').unlink()
    require(before == native_gate.identity(source)
            and all(native_gate.identity(Path(name)) == row for name, row in held.items())
            and private == [native_gate.identity(Path(name)) for name in context['assets']]
            and policy_sha == native_launch.digest(policy.data), 'inputs changed before publication')
    tool.verify()
    result = {'schema_version': 1, 'kind': KIND, 'profile': PROFILE, 'root': str(root),
        'source': str(source), 'tool': receipt, 'compile_context': context, 'context_inputs': private,
        'policy_sha256': policy_sha, 'inputs': list(held.values()), 'commands': [query, command],
        'semantic': native_gate.identity(compressed), 'semantic_sha256': hashlib.sha256(raw).hexdigest(),
        'semantic_bytes': len(raw), 'source_complete': False, **facts}
    (output / MANIFEST).write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    return result
