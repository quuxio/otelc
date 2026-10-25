"""Explicit per-unit native compilation, with separate neutral tooling inputs."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from otelc import llvm_backend, native_gate, native_launch, native_object_build
from otelc.config import Policy
from otelc.python_sdk_io import decode

PROFILE = 'llvm22-native-units-v1'
RECIPE_KIND = 'otelc.native.unit-recipe'
PATH_FLAGS = frozenset(('-I', '-isystem', '-iquote', '-include', '-imacros'))
VALUE_FLAGS = frozenset(('-D', '-U'))
SIMPLE_FLAGS = frozenset(('-g', '-g0', '-g1', '-g2', '-g3', '-gline-tables-only',
    '-pthread', '-pipe', '-fPIC', '-fpic', '-fPIE', '-fpie', '-fno-common', '-fcommon',
    '-fexceptions', '-fno-exceptions', '-frtti', '-fno-rtti',
    '-fno-omit-frame-pointer', '-fomit-frame-pointer', '-fno-strict-aliasing', '-fstrict-aliasing'))
SUFFIXES = {'c': ('.c',), 'cpp': ('.cc', '.cpp', '.cxx', '.C')}


def require(condition: bool, reason: str) -> None:
    """Reject an unsupported recipe before invoking any application compiler."""
    if not condition:
        raise ValueError('native unit recipe: ' + reason)


def path(value: Any, *, directory: bool = False) -> Path:
    """Recipes use explicit canonical paths, not metadata-selected aliases."""
    require(type(value) is str and bool(value) and '\0' not in value, 'invalid path')
    result = Path(value)
    require(result.is_absolute() and result.resolve(strict=True) == result
            and (result.is_dir() if directory else result.is_file()), 'noncanonical or missing input')
    return result


def flags(values: Any, cwd: Path) -> None:
    """Keep supported semantic flags in order; otelc owns action/output/plugins."""
    require(type(values) is list and len(values) <= 1024
            and all(type(value) is str and 0 < len(value) <= 8192 and '\0' not in value for value in values),
            'invalid compiler flags')
    index = 0
    while index < len(values):
        value = values[index]
        if value in PATH_FLAGS | VALUE_FLAGS:
            require(index + 1 < len(values), 'missing compiler option value')
            if value in PATH_FLAGS:
                selected = (cwd / values[index + 1]).resolve(strict=True)
                require(selected.is_file() if value in ('-include', '-imacros') else selected.is_dir(),
                        'invalid compiler path option')
            index += 2
            continue
        require(value in SIMPLE_FLAGS or re.fullmatch(r'-O[0123sgz]', value) is not None
                or re.fullmatch(r'-std=(?:gnu\+\+|c\+\+)(?:17|20|23)|-std=(?:gnu|c)(?:11|17|23)', value) is not None
                or re.fullmatch(r'-W(?:all|extra|error|pedantic|no-[A-Za-z][A-Za-z\d-]*)', value, re.ASCII) is not None,
                'unsupported compiler flag: ' + value)
        index += 1


def overlay(filename: Path, root: Path, assets: set[Path]) -> None:
    """Bind the simple private file overlays used by independent observation tools."""
    require(filename in assets and filename.stat().st_size <= 1024 * 1024, 'unbound or oversized VFS overlay')
    value = decode(filename.read_bytes())
    require(type(value) is dict and set(value) == {'version', 'use-external-names', 'roots'}
            and type(value['version']) is int and value['version'] == 0
            and value['use-external-names'] is False and type(value['roots']) is list
            and 1 <= len(value['roots']) <= 256, 'unsupported VFS overlay shape')
    names = set()
    for row in value['roots']:
        require(type(row) is dict and set(row) == {'type', 'name', 'external-contents'}
                and row['type'] == 'file', 'unsupported VFS entry')
        original, private = path(row['name']), path(row['external-contents'])
        require(original.is_relative_to(root) and private in assets and not private.is_relative_to(root)
                and original not in names, 'VFS input must bind original and distinct private bytes')
        names.add(original)


def validate(value: Any, root: Path) -> dict[str, Any]:
    """Copy one closed declarative recipe; no stored command strings are run."""
    require(type(value) is dict and set(value) == {'schema_version', 'kind', 'units', 'assets', 'link'}
            and type(value['schema_version']) is int and value['schema_version'] == 1
            and value['kind'] == RECIPE_KIND, 'unsupported schema')
    require(len(json.dumps(value, allow_nan=False).encode()) <= 1024 * 1024, 'recipe exceeds byte budget')
    require(type(value['assets']) is list and len(value['assets']) <= 1024, 'invalid private assets')
    assets = [path(item) for item in value['assets']]
    require(len(set(assets)) == len(assets) and all(not item.is_relative_to(root) for item in assets),
            'private assets must be distinct and outside application source')
    require(type(value['units']) is list and 1 <= len(value['units']) <= 257, 'invalid unit count')
    identifiers, contexts, applications, drivers = set(), set(), 0, 0
    for unit in value['units']:
        require(type(unit) is dict and set(unit) == {'id', 'role', 'source', 'language', 'directory',
                'flags', 'overlay', 'rename_main'}, 'invalid unit fields')
        identifier = unit['id']
        require(type(identifier) is str and re.fullmatch(r'[A-Za-z_]\w{0,63}', identifier, re.ASCII) is not None
                and identifier != 'link' and identifier not in identifiers, 'invalid or repeated unit identity')
        identifiers.add(identifier)
        require(type(unit['language']) is str and unit['language'] in SUFFIXES
                and unit['role'] in ('application', 'driver')
                and type(unit['rename_main']) is bool, 'unsupported unit language, role or rename')
        source, cwd = path(unit['source']), path(unit['directory'], directory=True)
        require(source.suffix in SUFFIXES[unit['language']], 'source language/suffix mismatch')
        flags(unit['flags'], cwd)
        if unit['role'] == 'application':
            applications += 1
            require(source.is_relative_to(root) and cwd.is_relative_to(root), 'application unit escapes its root')
            if unit['overlay'] is not None:
                overlay(path(unit['overlay']), root, set(assets))
        else:
            drivers += 1
            require(source in assets and unit['overlay'] is None and not unit['rename_main'],
                    'neutral driver must be an unmodified private asset')
        context = (str(source), unit['language'], str(cwd), tuple(unit['flags']), unit['overlay'], unit['role'])
        require(context not in contexts, 'repeated compile context')
        contexts.add(context)
    require(1 <= applications <= 256 and drivers <= 1, 'requires application units and at most one neutral driver')
    link = value['link']
    require(type(link) is dict and set(link) == {'language', 'directory', 'flags'}
            and type(link['language']) is str and link['language'] in SUFFIXES
            and type(link['flags']) is list and len(link['flags']) <= 256
            and all(type(flag) is str and (flag == '-pthread' or re.fullmatch(r'-l[A-Za-z_]\w*', flag, re.ASCII))
                    for flag in link['flags']), 'unsupported link context')
    path(link['directory'], directory=True)
    return json.loads(json.dumps(value, allow_nan=False))


def steps(recipe: dict[str, Any], output: Path, tools: dict[str, Any], plugin: str,
          library: Path, *, object_lifetimes: bool = False) -> list[dict[str, Any]]:
    """Derive the two matched compiler/link recipes, preserving explicit unit order."""
    binaries = Path(tools['directory'])
    result = []
    def append(label: str, directory: str, command: list[str], product: Path, **extra: Any) -> None:
        result.append({'label': label, 'directory': directory, 'arguments': command,
                       'output': str(product), **extra})
    for lane in ('plain', 'instrumented'):
        objects = []
        for unit in recipe['units']:
            label = lane + '-' + unit['id']
            final = output / (label + '.o')
            compiled = output / (label + '.original.o') if unit['rename_main'] else final
            dependencies = output / (label + '.d')
            compiler = binaries / ('clang' if unit['language'] == 'c' else 'clang++')
            command = [str(compiler), *unit['flags'], '-g', '-MD', '-MF', str(dependencies),
                       '-MT', 'otelc-inputs', '-c', unit['source'], '-o', str(compiled)]
            if unit['overlay'] is not None:
                command.extend(['-ivfsoverlay', unit['overlay']])
            if lane == 'instrumented' and unit['role'] == 'application':
                command.append('-fpass-plugin=' + plugin)
            extra = {'environment': native_object_build.environment(unit, output)} if (
                object_lifetimes and lane == 'instrumented' and unit['role'] == 'application') else {}
            append(label, unit['directory'], command, compiled, source=unit['source'],
                   dependencies=str(dependencies), unit=unit['id'], lane=lane, **extra)
            if unit['rename_main']:
                command = [str(binaries / 'llvm-objcopy'), '--redefine-sym', 'main=__oteleq_original_main',
                           '--redefine-sym', '_main=___oteleq_original_main', str(compiled), str(final)]
                append(label + '-rename', str(output), command, final)
            objects.append(str(final))
        if lane == 'instrumented':
            bootstrap = output / 'bootstrap.o'
            command = [str(binaries / 'clang++'), '-std=c++17', '-O2', '-g', '-c', str(output / 'bootstrap.cpp'),
                       '-o', str(bootstrap), '-I', str(native_launch.ROOT / 'adapters/native/launcher/include')]
            append('bootstrap', str(output), command, bootstrap, source=str(output / 'bootstrap.cpp'))
            objects.insert(0, str(bootstrap))
        binary = output / lane
        compiler = binaries / ('clang' if recipe['link']['language'] == 'c' else 'clang++')
        command = [str(compiler), *objects, *recipe['link']['flags'], '-o', str(binary)]
        if lane == 'instrumented':
            command.extend([str(library), '-Wl,-rpath,' + str(library.parent)])
        append(lane + '-link', recipe['link']['directory'], command, binary)
    return result


def build(policy: Policy, language: str, root: Path, recipe: dict[str, Any], output: Path,
          *, library: Path, llvm_config: Path | None = None) -> native_launch.Build:
    """Compile a caller-selected context without editing its application inputs."""
    objects = policy.data['lifetimes']['enabled']
    native_launch.validate_policy(policy, language, object_lifetimes=objects)
    root = root.resolve(strict=True)
    require(root.is_dir() and root != Path('/'), 'invalid application root')
    recipe = validate(recipe, root)
    require(not output.resolve().is_relative_to(root), 'output must be outside application source')
    held = {str(item): native_gate.identity(item) for item in
            {*(Path(row['source']) for row in recipe['units']), *map(Path, recipe['assets'])}}
    library = library.resolve(strict=True)
    sdk = native_launch.identity(library)
    output.mkdir(mode=0o700)
    output = output.resolve(strict=True)
    object_records = native_object_build.build(policy, root, recipe, output, llvm_config=llvm_config) if objects else {}
    plugin = llvm_backend.build(output / 'plugin', config=llvm_config, function_registry=True,
                                source_aware=True, object_lifetimes=objects)
    manifest = llvm_backend.verify(plugin)
    tools = manifest['toolchain']
    extra_tools = {}
    if any(row['rename_main'] for row in recipe['units']):
        objcopy = Path(tools['directory']) / 'llvm-objcopy'
        extra_tools['objcopy'] = native_gate.identity(objcopy)
        require(tools['version'] in llvm_backend.query(objcopy, '--version'), 'object tool differs from LLVM')
    (output / 'bootstrap.cpp').write_text(native_launch.BOOTSTRAP)
    commands = steps(recipe, output, tools, manifest['plugin']['path'], library, object_lifetimes=objects)
    dependencies, inputs = {}, {}
    for command in commands:
        environment = llvm_backend.environment(policy, language, source_root=root)
        environment.update(command.get('environment', {}))
        native_launch._compile(command['arguments'], Path(command['directory']), output, command['label'], environment)
        if 'dependencies' in command:
            rows = native_launch._inputs(Path(command['dependencies']), Path(command['directory']))
            dependencies[command['label']] = rows
            for row in rows:
                previous = inputs.setdefault(row['path'], row)
                require(previous == row, 'compiler input changed across units or lanes')
                held.setdefault(row['path'], native_gate.identity(Path(row['path'])))
    for unit in recipe['units']:
        require(dependencies['plain-' + unit['id']] == dependencies['instrumented-' + unit['id']],
                'compiler inputs differ between lanes')
    database = [{'directory': row['directory'], 'file': row['source'], 'arguments': row['arguments']}
                for row in commands if 'source' in row]
    (output / 'compile_commands.json').write_text(json.dumps(database, indent=2) + '\n')
    artifacts = {output / 'bootstrap.cpp', output / 'compile_commands.json'}
    if objects:
        artifacts.update(native_object_build.artifacts(recipe, output))
    for row in commands:
        artifacts.update((Path(row['output']), output / (row['label'] + '.stdout'), output / (row['label'] + '.stderr')))
        if 'dependencies' in row:
            artifacts.add(Path(row['dependencies']))
    value = {'schema_version': 3 if objects else 2, 'kind': 'otelc.native-build',
        'profile': native_object_build.PROFILE if objects else PROFILE,
        'language': language, 'root': str(root), 'policy': policy.data,
        'policy_sha256': native_launch.digest(policy.data),
        'selection_sha256': native_launch.digest(native_launch.selection(policy.data, object_lifetimes=objects)),
        'inputs': list(inputs.values()), 'sdk_library': sdk,
        'products': [native_launch.identity(output / lane) for lane in ('plain', 'instrumented')],
        'commands': commands, 'recipe': recipe, 'files': list(held.values()), 'tools': extra_tools,
        'artifacts': [native_gate.identity(item) for item in sorted(artifacts)],
        'dependencies': dependencies, 'source_complete': False, **object_records}
    encoded = json.dumps(value, sort_keys=True, indent=2) + '\n'
    (output / 'native-build.json').write_text(encoded)
    result = native_launch.Build(output, encoded, plugin)
    native_launch.verify(result)
    return result


def verify(built: native_launch.Build, value: dict[str, Any]) -> None:
    """Check retained contexts and products without executing recorded commands."""
    recipe = validate(value['recipe'], Path(value['root']))
    objects = value['profile'] == native_object_build.PROFILE
    plugin = llvm_backend.verify(built.plugin)
    require(value['commands'] == steps(recipe, built.directory, plugin['toolchain'],
            plugin['plugin']['path'], Path(value['sdk_library']['path']), object_lifetimes=objects),
            'retained compiler context changed')
    require(type(value['dependencies']) is dict and type(value['tools']) is dict
            and all(type(value[name]) is list for name in ('files', 'artifacts')), 'invalid retained record collections')
    require(all(type(row) is dict and type(row.get('path')) is str
                for row in [*value['files'], *value['artifacts'], *value['tools'].values()]),
            'invalid retained file identity')
    required_files = {row['source'] for row in recipe['units']} | set(recipe['assets'])
    expected_dependencies = {lane + '-' + unit['id'] for lane in ('plain', 'instrumented') for unit in recipe['units']}
    require(set(value['dependencies']) == expected_dependencies, 'incomplete per-unit dependency denominator')
    dependency_inputs = {}
    for rows in value['dependencies'].values():
        require(type(rows) is list and 1 <= len(rows) <= 10000
                and all(type(row) is dict and type(row.get('path')) is str for row in rows),
                'invalid dependency denominator')
        required_files.update(row['path'] for row in rows)
        for row in rows:
            require(dependency_inputs.setdefault(row['path'], row) == row, 'conflicting dependency identities')
    require(len(value['inputs']) == len(dependency_inputs)
            and {row['path']: row for row in value['inputs']} == dependency_inputs,
            'incomplete compiler input denominator')
    require(len(value['files']) == len(required_files)
            and {row['path'] for row in value['files']} == required_files, 'incomplete input denominator')
    expected_artifacts = {str(built.directory / name) for name in ('bootstrap.cpp', 'compile_commands.json')}
    if objects:
        expected_artifacts.update(map(str, native_object_build.artifacts(recipe, built.directory)))
    for row in value['commands']:
        expected_artifacts.update((row['output'], str(built.directory / (row['label'] + '.stdout')),
                                   str(built.directory / (row['label'] + '.stderr'))))
        if 'dependencies' in row:
            expected_artifacts.add(row['dependencies'])
    require(len(value['artifacts']) == len(expected_artifacts)
            and {row['path'] for row in value['artifacts']} == expected_artifacts, 'incomplete compiler channel denominator')
    renamed = any(row['rename_main'] for row in recipe['units'])
    require(set(value['tools']) == ({'objcopy'} if renamed else set()), 'unexpected object tool denominator')
    if renamed:
        require(value['tools']['objcopy']['path'] == str(Path(plugin['toolchain']['directory']) / 'llvm-objcopy'),
                'object tool differs from matched compiler')
    for row in [*value['files'], *value['artifacts'], *value['tools'].values()]:
        require(native_gate.identity(Path(row['path'])) == row, 'input, tool or retained compiler channel changed')
    for unit in recipe['units']:
        for lane in ('plain', 'instrumented'):
            label = lane + '-' + unit['id']
            require(native_launch._inputs(built.directory / (label + '.d'), Path(unit['directory'])) == value['dependencies'][label],
                    'retained per-unit dependency manifest changed')
    expected_database = [{'directory': row['directory'], 'file': row['source'], 'arguments': row['arguments']}
                         for row in value['commands'] if 'source' in row]
    require(json.loads((built.directory / 'compile_commands.json').read_bytes()) == expected_database,
            'retained compilation database differs from executed commands')
    if objects:
        native_object_build.verify(value, built.directory)
