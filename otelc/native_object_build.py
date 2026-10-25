"""Bind per-unit Clang lifetime inventories to the public native build recipe."""
from __future__ import annotations

import gzip
import hashlib
from pathlib import Path
import re
from typing import Any

from otelc import native_gate, native_launch, native_lifetimes
from otelc.config import Policy
from otelc.python_sdk_io import decode

PROFILE = 'llvm22-native-object-units-v1'
TOOL_FILES = ('tool.d', 'tool-dependencies.json', 'tool-dependencies.stderr',
              'tool-build.json', 'tool-build.stderr', 'tool-dependencies-after.json',
              'tool-dependencies-after.stderr', native_lifetimes.MANIFEST,
              'compile_commands.json', 'lifetimes')
UNIT_FILES = ('0.d', '0-dependencies.json', '0-dependencies.stderr', '0-semantics.stderr',
              '0-dependencies-after.json', '0-dependencies-after.stderr',
              '0-semantics.json.gz', 'native-lifetimes.json')


def require(condition: bool, reason: str) -> None:
    """A changed or incomplete inventory cannot authorise a lifetime launch."""
    if not condition:
        raise ValueError('native object build: ' + reason)


def units(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    """Only application C++ units have C++ construction semantics."""
    return [unit for unit in recipe['units'] if unit['role'] == 'application' and unit['language'] == 'cpp']


def context(recipe: dict[str, Any], unit: dict[str, Any]) -> dict[str, Any]:
    """Keep the exact caller-selected context, including all declared private assets."""
    return {key: unit[key] for key in ('directory', 'flags', 'overlay')} | {'assets': recipe['assets']}


def artifacts(recipe: dict[str, Any], output: Path) -> set[Path]:
    """Retain the complete fixed helper/query channel denominator."""
    paths = {output / 'lifetime-tool' / name for name in TOOL_FILES}
    for unit in units(recipe):
        paths.update(output / 'lifetimes' / unit['id'] / name for name in UNIT_FILES)
    return paths


def build(policy: Policy, root: Path, recipe: dict[str, Any], output: Path,
          *, llvm_config: Path | None = None) -> dict[str, Any]:
    """Inventory each actual C++ context without rewriting application source."""
    tool = native_lifetimes.build_tool(output / 'lifetime-tool', llvm_config=llvm_config)
    (output / 'lifetimes').mkdir(mode=0o700)
    inventories = {}
    for unit in units(recipe):
        directory = output / 'lifetimes' / unit['id']
        native_lifetimes.inspect(policy, root, [Path(unit['source'])], directory,
                                tool=tool, context=context(recipe, unit))
        inventories[unit['id']] = native_gate.identity(directory / 'native-lifetimes.json')
    return {'object_tool': native_gate.identity(tool.directory / native_lifetimes.MANIFEST),
            'object_inventories': inventories}


def environment(unit: dict[str, Any], output: Path) -> dict[str, str]:
    """Retain the actual per-unit object environment without unrelated host data."""
    if unit['language'] == 'c':
        return {'OTELC_OBJECT_C_UNIT': '1'}
    return {'OTELC_OBJECT_INVENTORY': str(output / 'lifetimes' / unit['id'] / 'native-lifetimes.json')}


def verify(value: dict[str, Any], output: Path) -> None:
    """Reconstruct inventory context and all semantic/input bindings without tools."""
    policy = Policy.parse(value['policy'])
    require(value['language'] == 'cpp' and policy.data['lifetimes']['enabled'] is True,
            'object profile requires enabled C++ lifetimes')
    recipe, root = value['recipe'], Path(value['root'])
    tool_path = output / 'lifetime-tool' / native_lifetimes.MANIFEST
    require(value['object_tool'] == native_gate.identity(tool_path), 'semantic tool binding changed')
    tool = native_lifetimes.Tool(tool_path.parent, tool_path.read_text())
    tool_record = tool.verify()
    selected = units(recipe)
    require(type(value['object_inventories']) is dict
            and set(value['object_inventories']) == {unit['id'] for unit in selected},
            'incomplete semantic unit denominator')
    fields = {'schema_version', 'kind', 'profile', 'root', 'target', 'tool', 'policy_sha256',
              'inputs', 'commands', 'translation_units', 'source_complete', 'compile_context', 'context_inputs'}
    for unit in selected:
        directory = output / 'lifetimes' / unit['id']
        filename = directory / 'native-lifetimes.json'
        require(value['object_inventories'][unit['id']] == native_gate.identity(filename),
                'semantic inventory binding changed')
        document = decode(filename.read_bytes())
        require(type(document) is dict and set(document) == fields
                and type(document['schema_version']) is int and document['schema_version'] == 2
                and document['kind'] == 'otelc.native.lifetime-inventory'
                and document['profile'] == native_lifetimes.CONTEXT_PROFILE
                and document['source_complete'] is False and document['root'] == str(root)
                and document['tool'] == tool_record
                and document['compile_context'] == context(recipe, unit)
                and document['policy_sha256'] == native_launch.digest(value['policy'])
                and type(document['target']) is str
                and re.fullmatch(r'[A-Za-z0-9_.-]+', document['target']) is not None
                and ('-apple-' in document['target'] or '-linux-' in document['target']),
                'semantic profile, context or policy changed')
        require(document['context_inputs'] == [native_gate.identity(Path(name)) for name in recipe['assets']],
                'semantic private inputs changed')
        inputs = document['inputs']
        require(type(inputs) is list and all(type(row) is dict and type(row.get('path')) is str for row in inputs),
                'invalid semantic dependencies')
        expected = {row['path'] for row in value['dependencies']['plain-' + unit['id']]}
        require(len(inputs) == len(expected) and {row['path'] for row in inputs} == expected
                and all(native_gate.identity(Path(row['path'])) == row for row in inputs),
                'semantic dependencies differ from actual compiler inputs')
        common = [str(Path(tool_record['toolchain']['directory']) / 'clang++'),
                  '--target=' + document['target'], *unit['flags'], '-g']
        if unit['overlay'] is not None:
            common += ['-ivfsoverlay', unit['overlay']]
        query = [*common, '-M', '-MT', 'otelc-inputs', '-MF', str(directory / '0.d'), unit['source']]
        semantic = [str(tool.directory / 'lifetimes'), str(root), unit['source'],
                    '--compile-directory', unit['directory'], *common]
        require(document['commands'] == [query, semantic], 'semantic compiler commands changed')
        require(type(document['translation_units']) is list and len(document['translation_units']) == 1,
                'semantic record must identify exactly one source unit')
        row = document['translation_units'][0]
        require(type(row) is dict and set(row) == {'source', 'types', 'constructions', 'variables', 'unresolved',
                'semantic', 'semantic_sha256', 'semantic_bytes'} and row['source'] == unit['source']
                and row['semantic'] == native_gate.identity(directory / '0-semantics.json.gz')
                and type(row['semantic_bytes']) is int and 0 < row['semantic_bytes'] <= native_lifetimes.MAX_AST,
                'semantic source or retained data changed')
        with gzip.open(directory / '0-semantics.json.gz', 'rb') as stream:
            raw = stream.read(native_lifetimes.MAX_AST + 1)
        require(len(raw) == row['semantic_bytes'] and hashlib.sha256(raw).hexdigest() == row['semantic_sha256'],
                'semantic data differs from its bounded digest')
        facts = native_lifetimes.catalogue(decode(raw), policy, root)
        require(all(row[key] == facts[key] for key in ('types', 'constructions', 'variables', 'unresolved')),
                'semantic selection differs from actual Clang records')
