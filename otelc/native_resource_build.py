"""Retain C allocation semantics and actual post-optimisation compiler boundaries."""
from __future__ import annotations

import gzip
import hashlib
import os
from pathlib import Path
from typing import Any

from otelc import native_gate, native_launch, native_lifetimes, native_object_build, native_resources
from otelc.config import Policy
from otelc.python_sdk_io import decode

PROFILE = 'llvm22-native-resource-units-v1'
UNIT_FILES = ('source.d', 'dependencies.json', 'dependencies.stderr', 'semantics.stderr',
              'dependencies-after.json', 'dependencies-after.stderr', 'semantics.json.gz',
              native_resources.MANIFEST, 'resolution.json')


def require(condition: bool, reason: str) -> None:
    """Incomplete compiler evidence cannot authorise a resource launch."""
    if not condition:
        raise ValueError('native resource build: ' + reason)


def units(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    """This C profile has no implicit C++ allocation semantics."""
    selected = [unit for unit in recipe['units'] if unit['role'] == 'application']
    require(bool(selected) and all(unit['language'] == 'c' for unit in selected),
            'resource profile requires C application units')
    return selected


def artifacts(recipe: dict[str, Any], output: Path) -> set[Path]:
    """Keep the fixed tool, semantic query channels and emitted-site denominator."""
    paths = {output / 'resource-tool' / name for name in native_object_build.TOOL_FILES}
    for unit in units(recipe):
        paths.update(output / 'resources' / unit['id'] / name for name in UNIT_FILES)
    return paths


def build(policy: Policy, root: Path, recipe: dict[str, Any], output: Path,
          *, llvm_config: Path | None = None) -> dict[str, Any]:
    """Inspect each unedited unit with its actual flags, cwd and private assets."""
    selected = units(recipe)
    tool = native_lifetimes.build_tool(output / 'resource-tool', llvm_config=llvm_config,
                                       profile=native_resources.PROFILE)
    (output / 'resources').mkdir(mode=0o700)
    inventories = {}
    for unit in selected:
        directory = output / 'resources' / unit['id']
        native_resources.inspect(policy, root, Path(unit['source']), directory, tool=tool,
                                 context=native_object_build.context(recipe, unit))
        inventories[unit['id']] = native_gate.identity(directory / native_resources.MANIFEST)
    return {'resource_tool': native_gate.identity(tool.directory / native_lifetimes.MANIFEST),
            'resource_inventories': inventories}


def environment(unit: dict[str, Any], output: Path) -> dict[str, str]:
    """Compiler output is a new fixed-name file in the owned unit directory."""
    directory = output / 'resources' / unit['id']
    return {'OTELC_RESOURCE_INVENTORY': str(directory / native_resources.MANIFEST),
            'OTELC_RESOURCE_RESOLUTION': str(directory / 'resolution.json')}


def resolution(document: dict[str, Any], directory: Path) -> None:
    """Bind emitted calls to the original ordered source plan, including zero matches."""
    value = decode((directory / 'resolution.json').read_bytes())
    require(type(value) is dict and set(value) == {'schema_version', 'kind', 'source', 'sites'}
            and type(value['schema_version']) is int and value['schema_version'] == 1
            and value['kind'] == 'otelc.native.c-resource-resolution'
            and value['source'] == document['source'] and type(value['sites']) is list,
            'invalid emitted-site resolution')
    original = [*document['allocations'], *document['releases']]
    require(len(value['sites']) == len(original), 'incomplete emitted-site denominator')
    for row, site in zip(value['sites'], original, strict=True):
        location = site['location']
        path = Path(location['debug_file'])
        if not path.is_absolute():
            path = Path(document['compile_context']['directory']) / path
        expected = {'caller': site['caller'], 'file': os.path.normpath(path),
            'line': location['debug_line'], 'column': location['debug_column'],
            'callee': site['callee'], 'type_usr': site.get('type_usr', '')}
        require(type(row) is dict and set(row) == set(expected) | {'emitted_calls', 'emitted_callees'}
                and all(type(row[key]) is type(item) and row[key] == item for key, item in expected.items())
                and type(row['emitted_calls']) is int and 0 <= row['emitted_calls'] <= 1048576
                and type(row['emitted_callees']) is list
                and len(row['emitted_callees']) == row['emitted_calls']
                and all(name in (('free',) if site['callee'] == 'free' else ('malloc', 'calloc'))
                        for name in row['emitted_callees']), 'emitted compiler site changed')


def verify(value: dict[str, Any], output: Path) -> None:
    """Reconstruct all input and selection bindings without running metadata commands."""
    policy, recipe, root = Policy.parse(value['policy']), value['recipe'], Path(value['root'])
    require(value['language'] == 'c' and policy.data['lifetimes']['enabled'],
            'resource profile requires enabled C lifetimes')
    tool_path = output / 'resource-tool' / native_lifetimes.MANIFEST
    require(value['resource_tool'] == native_gate.identity(tool_path), 'resource tool binding changed')
    tool = native_lifetimes.Tool(tool_path.parent, tool_path.read_text(), native_resources.PROFILE)
    receipt = tool.verify()
    selected = units(recipe)
    require(type(value['resource_inventories']) is dict
            and set(value['resource_inventories']) == {unit['id'] for unit in selected},
            'incomplete resource unit denominator')
    for unit in selected:
        _verify_unit(value, output, tool, receipt, unit)


def _verify_unit(value: dict[str, Any], output: Path, tool: native_lifetimes.Tool,
                 receipt: dict[str, Any], unit: dict[str, Any]) -> None:
    """Check one closed unit record separately from the complete unit denominator."""
    policy, recipe, root = Policy.parse(value['policy']), value['recipe'], Path(value['root'])
    directory = output / 'resources' / unit['id']
    path = directory / native_resources.MANIFEST
    require(value['resource_inventories'][unit['id']] == native_gate.identity(path),
            'resource inventory binding changed')
    document = decode(path.read_bytes())
    expected = {'schema_version': 1, 'kind': native_resources.KIND, 'profile': native_resources.PROFILE,
        'root': str(root), 'source': unit['source'], 'tool': receipt, 'source_complete': False,
        'compile_context': native_object_build.context(recipe, unit),
        'context_inputs': [native_gate.identity(Path(name)) for name in recipe['assets']],
        'policy_sha256': native_launch.digest(value['policy']),
        'semantic': native_gate.identity(directory / 'semantics.json.gz')}
    require(type(document) is dict and set(document) == set(expected) | {
            'inputs', 'commands', 'semantic_sha256', 'semantic_bytes', 'types', 'allocations', 'releases'}
            and all(native_launch.digest(document[key]) == native_launch.digest(item)
                    for key, item in expected.items()), 'resource profile, context or selection changed')
    require(type(document['semantic_bytes']) is int and 0 < document['semantic_bytes'] <= native_lifetimes.MAX_AST,
            'invalid retained C semantic size')
    with gzip.open(directory / 'semantics.json.gz', 'rb') as stream:
        raw = stream.read(native_lifetimes.MAX_AST + 1)
    require(len(raw) == document['semantic_bytes']
            and hashlib.sha256(raw).hexdigest() == document['semantic_sha256'], 'retained C semantics changed')
    facts = native_resources.catalogue(decode(raw), policy, root)
    require(all(document[key] == item for key, item in facts.items()), 'resource selection differs from Clang')
    inputs = document['inputs']
    expected_inputs = value['dependencies']['plain-' + unit['id']]
    require(type(inputs) is list and len(inputs) == len(expected_inputs)
            and {row['path'] for row in inputs} == {row['path'] for row in expected_inputs}
            and all(native_gate.identity(Path(row['path'])) == row for row in inputs)
            and all(native_launch.identity(Path(row['path'])) == row for row in expected_inputs),
            'resource dependencies differ from actual compiler inputs')
    common = [str(Path(receipt['toolchain']['directory']) / 'clang'), *unit['flags'], '-g']
    if unit['overlay'] is not None:
        common += ['-ivfsoverlay', unit['overlay']]
    commands = [[*common, '-M', '-MT', 'otelc-inputs', '-MF', str(directory / 'source.d'), unit['source']],
        [str(tool.directory / 'lifetimes'), str(root), unit['source'],
         '--compile-directory', unit['directory'], *common]]
    require(document['commands'] == commands, 'resource compiler commands changed')
    resolution(document, directory)
