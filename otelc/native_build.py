"""Versioned native build bindings, readable without invoking a compiler."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from otelc import file_inputs, llvm_backend, native_gate, native_launch, native_units, native_object_build, native_resource_build
from otelc.config import Policy
from otelc.python_sdk_io import decode

PROFILE = 'native-owned-v1'
MANIFEST = 'native-source-build.json'
SDK_MANIFEST = 'native-sdk-build.json'
FIELDS = {'schema_version', 'kind', 'profile', 'native', 'plugin', 'gate', 'sdk', 'source_complete'}


def read(path: Path) -> tuple[dict[str, Any], bytes]:
    """Read bounded build data without evaluating commands from that data."""
    _, raw = file_inputs.checked_file(path, nofollow=True, include_content=True, max_bytes=16 * 1024 * 1024)
    return decode(raw), raw


def sdk(directory: Path) -> dict[str, Any]:
    """Bind the fixed SDK product and its retained source/tool inputs."""
    directory = directory.resolve(strict=True)
    value, _ = read(directory / SDK_MANIFEST)
    fields = {'schema_version', 'kind', 'sources', 'toolchain', 'build_tools', 'commands',
              'library', 'coverage', 'source_complete'}
    name = 'libotelc_native_runtime.dylib' if sys.platform == 'darwin' else 'libotelc_native_runtime.so'
    if (set(value) != fields or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['kind'] != 'otelc.native-sdk-build' or value['source_complete'] is not False
            or type(value['coverage']) is not bool or type(value['toolchain']) is not dict
            or type(value['build_tools']) is not list or len(value['build_tools']) != 2
            or any(type(row) is not dict or type(row.get('path')) is not str for row in value['build_tools'])
            or value['library'] != native_launch.identity(directory / name)
            or not llvm_backend.unchanged(value['toolchain'])):
        raise ValueError('native prepared profile requires the fixed SDK build record/product')
    source = native_launch.ROOT / 'adapters/native'
    expected = [source / 'CMakeLists.txt', source / 'launcher/CMakeLists.txt',
                *sorted(source.rglob('*.cpp')), *sorted(source.rglob('*.h')), llvm_backend.HEADER, native_launch.ROOT / 'native/llvm/object_abi.h', native_launch.ROOT / 'native/llvm/resource_abi.h']
    if value['sources'] != [native_launch.identity(path) for path in expected]:
        raise ValueError('native SDK does not bind the current fixed source recipe')
    for record in value['build_tools']:
        if native_launch.identity(Path(record['path'])) != record:
            raise ValueError('native SDK build tool changed')
    return value


def build(policy: Policy, language: str, root: Path, sources: list[Path], output: Path,
          *, sdk_directory: Path, recipe: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build both lanes and the fixed gate, preserving original application files."""
    lifetimes = policy.data['lifetimes']['enabled']
    objects, resources = lifetimes and language == 'cpp', lifetimes and language == 'c'
    if lifetimes:
        native_launch.validate_policy(policy, language, object_lifetimes=objects, resource_lifetimes=resources)
        if recipe is None:
            root = root.resolve(strict=True)
            recipe = {'schema_version': 1, 'kind': native_units.RECIPE_KIND, 'assets': [],
                'units': [{'id': 'unit_' + str(index), 'role': 'application',
                    'source': str((root / source).resolve(strict=True)), 'language': language,
                    'directory': str(root), 'flags': ['-O2', '-fexceptions', '-pthread',
                        '-std=c11' if resources else '-std=c++17', '-I', str(root)],
                    'overlay': None, 'rename_main': False} for index, source in enumerate(sources)],
                'link': {'language': language, 'directory': str(root), 'flags': ['-pthread']}}
            sources = []
    source_sdk = sdk(sdk_directory)
    if recipe is None:
        built = native_launch.build(policy, language, root, sources, output,
                                    library=Path(source_sdk['library']['path']))
    else:
        if sources:
            raise ValueError('native build selects either fixed sources or an explicit unit recipe')
        built = native_units.build(policy, language, root, recipe, output,
                                  library=Path(source_sdk['library']['path']))
    native_gate.build(built.directory / 'gate')
    version = 2
    if recipe is not None:
        version = 3
    if objects:
        version = 4
    if resources:
        version = 5
    value = {'schema_version': version,
             'kind': 'otelc.native.source-build', 'profile': PROFILE,
             'native': native_gate.identity(built.directory / 'native-build.json'),
             'plugin': native_gate.identity(built.directory / 'plugin' / llvm_backend.MANIFEST),
             'gate': native_gate.identity(built.directory / 'gate/gate.json'),
             'sdk': native_gate.identity(Path(sdk_directory).resolve() / SDK_MANIFEST),
             'source_complete': False}
    (built.directory / MANIFEST).write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    return value


def verify(directory: Path, policy: Policy) -> tuple[native_launch.Build, native_gate.Product, dict[str, Any]]:
    """Reconstruct only fixed product paths and recheck all declared bindings."""
    directory = directory.resolve(strict=True)
    value, _ = read(directory / MANIFEST)
    if (set(value) != FIELDS or type(value['schema_version']) is not int or value['schema_version'] not in (2, 3, 4, 5)
            or value['kind'] != 'otelc.native.source-build' or value['profile'] != PROFILE
            or value['source_complete'] is not False):
        raise ValueError('native preparation requires source-build2,3,4 or5/native-owned-v1')
    fixed = {'native': directory / 'native-build.json', 'plugin': directory / 'plugin' / llvm_backend.MANIFEST,
             'gate': directory / 'gate/gate.json'}
    for field, path in fixed.items():
        if native_gate.identity(path) != value[field]:
            raise ValueError('native build binding changed: ' + field)
    if type(value['sdk']) is not dict or type(value['sdk'].get('path')) is not str:
        raise ValueError('native SDK build binding must identify its fixed record')
    sdk_path = Path(value['sdk']['path'])
    if (not sdk_path.is_absolute() or sdk_path.name != SDK_MANIFEST
            or sdk_path.resolve(strict=True) != sdk_path or native_gate.identity(sdk_path) != value['sdk']):
        raise ValueError('native SDK build binding changed')
    source_sdk = sdk(sdk_path.parent)
    metadata, raw = read(fixed['native'])
    plugin, plugin_raw = read(fixed['plugin'])
    gate, gate_raw = read(fixed['gate'])
    native_fields = {'schema_version', 'kind', 'profile', 'language', 'root', 'policy', 'policy_sha256',
                     'selection_sha256', 'inputs', 'sdk_library', 'products', 'commands', 'source_complete'}
    units = value['schema_version'] in (3, 4, 5)
    objects = value['schema_version'] == 4
    resources = value['schema_version'] == 5
    if units:
        native_fields |= {'recipe', 'files', 'artifacts', 'tools', 'dependencies'}
    if objects:
        native_fields |= {'object_tool', 'object_inventories'}
    if resources:
        native_fields |= {'resource_tool', 'resource_inventories'}
    expected_schema, expected_profile = {
        2: (1, 'llvm22-native-launch-v1'),
        3: (2, native_units.PROFILE),
        4: (3, native_object_build.PROFILE),
        5: (4, native_resource_build.PROFILE),
    }[value['schema_version']]
    if (set(metadata) != native_fields or type(metadata.get('schema_version')) is not int
            or metadata['schema_version'] != expected_schema or metadata.get('kind') != 'otelc.native-build'
            or metadata.get('profile') != expected_profile
            or metadata.get('language') not in ('c', 'cpp') or type(metadata.get('root')) is not str
            or type(metadata.get('policy')) is not dict
            or metadata.get('policy_sha256') != native_launch.digest(metadata['policy'])
            or type(metadata.get('inputs')) is not list or type(metadata.get('products')) is not list
            or any(type(row) is not dict or type(row.get('path')) is not str
                   for row in [*metadata['inputs'], *metadata['products']])
            or metadata.get('source_complete') is not False
            or type(plugin.get('schema_version')) is not int or plugin['schema_version'] != 1
            or plugin.get('kind') != 'otelc.llvm-plugin' or plugin.get('source_complete') is not False
            or plugin.get('profile') != (llvm_backend.RESOURCE_PROFILE if resources else llvm_backend.OBJECT_PROFILE if objects else llvm_backend.SOURCE_PROFILE)
            or objects and plugin.get('object_probe_abi') != 'otelc.object.v1'
            or resources and plugin.get('resource_probe_abi') != 'otelc.resource.v1'
            or plugin.get('probe_abi') != 'otelc.function.v2'
            or metadata.get('sdk_library') != source_sdk['library']
            or [row['path'] for row in metadata['products']] != [str(directory / name) for name in ('plain', 'instrumented')]
            or gate.get('kind') != 'otelc.native.exec-gate' or type(gate.get('schema_version')) is not int
            or gate['schema_version'] != 1 or gate['source_complete'] is not False
            or gate['executable']['path'] != str(directory / 'gate/otelc-exec-gate')
            or gate['source']['path'] != str(native_gate.SOURCE)):
        raise ValueError('native build does not use the fixed plugin, gate or SDK profile')
    built = native_launch.Build(directory, raw.decode(), llvm_backend.Plugin(directory / 'plugin', plugin_raw.decode()))
    native_launch.verify(built)
    held_gate = native_gate.Product(gate_raw.decode())
    held_gate.verify()
    native_launch.validate_policy(policy, metadata['language'], object_lifetimes=objects, resource_lifetimes=resources)
    if native_launch.digest(native_launch.selection(policy.data, object_lifetimes=objects, resource_lifetimes=resources)) != metadata['selection_sha256']:
        raise ValueError('native policy differs from compiled source/function selection')
    return built, held_gate, value
