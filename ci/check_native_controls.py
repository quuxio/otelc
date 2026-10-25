"""Build real C++ SDK controls and enforce their own LLVM coverage floor."""
from __future__ import annotations

import copy
import json
import resource
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from otelc import llvm_backend, native_lifetimes
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'adapters/native'


def summaries(data: dict[str, Any], expected: set[Path]) -> dict[str, Any]:
    """Require every actual control source exactly once, each above 80% lines."""
    files = [file for block in data['data'] for file in block['files']
             if Path(file['filename']).resolve() in expected]
    if len(files) != len(expected) or {Path(file['filename']).resolve() for file in files} != expected:
        raise ValueError('native metric coverage must identify each exact source once')
    result = {}
    for file in files:
        lines = file['summary']['lines']
        if lines['count'] == 0 or lines['percent'] < 80:
            raise ValueError('native metric control coverage is below 80%')
        result[file['filename']] = lines
    return result


def no_core() -> None:
    """Expected compiler refusals must not leave core dumps on the host."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def object_refusals(work: Path, inventory: dict[str, Any], manifest: dict[str, Any],
                    binaries: Path, environment: dict[str, str]) -> None:
    """Refuse incomplete or ambiguous semantic input before an app is linked."""
    cases = []
    for name, reason in (('schema', 'unsupported object inventory'),
                         ('source', 'expected one bound object translation unit'),
                         ('type', 'duplicate object type identity'),
                         ('ambiguous', 'ambiguous compiler object boundary'),
                         ('missing', 'selected constructor has no matching compiler boundary'),
                         ('trivial', 'separate boundary profile')):
        value = copy.deepcopy(inventory)
        unit = value['translation_units'][0]
        complete = next(row for row in unit['constructions'] if row['construction_kind'] == 'complete')
        if name == 'schema': value['schema_version'] = 2
        elif name == 'source': value['translation_units'] = []
        elif name == 'type': unit['types'].append(copy.deepcopy(unit['types'][0]))
        elif name == 'ambiguous': unit['constructions'].append(copy.deepcopy(complete))
        elif name == 'missing': complete['location']['debug_column'] += 10000
        else: complete['trivial'] = True
        path = work / ('refused-' + name + '.json')
        path.write_text(json.dumps(value) + '\n')
        cases.append((name, path, reason))
    cases.append(('absent', work / 'absent-inventory.json', 'object inventory unavailable'))
    results = []
    for name, path, reason in cases:
        command = [str(binaries / 'clang++'), '-std=c++17', '-g', '-O2', '-fexceptions',
                   '-fpass-plugin=' + manifest['plugin']['path'], '-c',
                   str(ROOT / 'tests/fixtures/native-objects.cpp'), '-o', str(work / ('refused-' + name + '.o'))]
        result = subprocess.run(command, env={**environment, 'OTELC_OBJECT_INVENTORY': str(path)},
                                capture_output=True, timeout=60, preexec_fn=no_core)
        (work / ('refused-' + name + '.stderr')).write_bytes(result.stderr)
        if not result.returncode or reason.encode() not in result.stderr:
            raise ValueError('native object refusal control did not reject: ' + name)
        results.append({'case': name, 'expected_diagnostic': reason, 'exit_code': result.returncode})
    (work / 'object-refusals.json').write_text(json.dumps(results, indent=2) + '\n')


def object_report(output: Path, work: Path, plugin: llvm_backend.Plugin,
                  manifest: dict[str, Any], binaries: Path, environment: dict[str, str]) -> None:
    """Keep the object pass's own exact source coverage and real compile context."""
    profiles = sorted(work.glob('object-compiler-*.profraw'))
    if not profiles:
        raise ValueError('object compiler execution produced no coverage profiles')
    merged = output / 'native-object-pass.profdata'
    subprocess.run([str(binaries / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles),
                    '-o', str(merged)], check=True, capture_output=True, timeout=60)
    source = llvm_backend.OBJECT_INPUTS[0]
    command = [str(binaries / 'llvm-cov'), 'export', manifest['plugin']['path'],
               '-instr-profile=' + str(merged), str(source)]
    data = json.loads(subprocess.check_output(command, env=environment, timeout=60))
    lines = summaries(data, {source})[str(source)]
    lcov = output / 'native-object-pass.lcov'
    lcov.write_bytes(subprocess.check_output([*command, '-format=lcov'], env=environment, timeout=60))
    database = output / 'native-object-pass-compile-commands.json'
    entries = json.loads((plugin.directory / 'compile_commands.json').read_text())
    entries = [row for row in entries if Path(row['file']) == source]
    if len(entries) != 1:
        raise ValueError('object compiler context must identify its actual translation unit')
    database.write_text(json.dumps(entries, indent=2) + '\n')
    product = output / Path(manifest['plugin']['path']).name.replace('libotelc_llvm', 'native-object-plugin')
    shutil.copyfile(manifest['plugin']['path'], product)
    llvm_backend.verify(plugin)
    (output / 'native-object-pass-coverage.json').write_text(json.dumps({
        'source': llvm_backend.identity(source), 'headers': manifest['object_inputs'][1:],
        'product': llvm_backend.identity(product), 'plugin': manifest,
        'lcov': llvm_backend.identity(lcov), 'compilation_database': llvm_backend.identity(database),
        'line_coverage': lines}, indent=2) + '\n')


def check() -> None:
    """Compile the pinned upstream SDK; retain compact results after real tests."""
    llvm = llvm_backend.discover()
    binaries = Path(llvm['directory'])
    cmake = shutil.which('cmake')
    ninja = shutil.which('ninja')
    if not cmake or not ninja:
        raise ValueError('native SDK checks require CMake 3.22+ and Ninja')
    cmake = str(Path(cmake).resolve(strict=True))
    ctest = Path(cmake).with_name('ctest')
    tools = [Path(cmake), Path(ninja).resolve(strict=True), ctest,
             binaries / 'llvm-cov', binaries / 'llvm-profdata']
    tool_inputs = [llvm_backend.identity(path) for path in tools]
    sources = [NATIVE / 'CMakeLists.txt', *sorted(NATIVE.rglob('*.cpp')),
               *sorted(NATIVE.rglob('*.c')), *sorted(NATIVE.rglob('*.h')),
               llvm_backend.HEADER, *llvm_backend.OBJECT_INPUTS, native_lifetimes.SOURCE,
               ROOT / 'tests/fixtures/native-objects.cpp', Path(__file__).resolve()]
    before = [llvm_backend.identity(path) for path in sources]
    expected = {path.resolve() for path in (NATIVE / 'src').glob('*.cpp')}
    if not expected:
        raise ValueError('native metric controls have no implementation sources')
    output = ROOT / 'build'
    output.mkdir(exist_ok=True)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('OTEL') and key not in ('CXXFLAGS', 'CFLAGS', 'CPPFLAGS', 'LDFLAGS')}
    commands = []
    keep_analysis = os.environ.get('OTELC_CPP_ANALYSIS') == '1'
    with tempfile.TemporaryDirectory(prefix='native-controls-', dir=output, delete=not keep_analysis) as temporary:
        work = Path(temporary).resolve()
        plugin = llvm_backend.build(work / 'plugin', function_registry=True)
        manifest = llvm_backend.verify(plugin)
        object_plugin = llvm_backend.build(work / 'object-plugin', function_registry=True,
            source_aware=True, object_lifetimes=True, coverage=True)
        object_manifest = llvm_backend.verify(object_plugin)
        object_policy = Policy.parse({'schema_version': 3, 'languages': ['cpp'],
            'sources': {'include': ['**']}, 'lifetimes': {
                'enabled': True, 'include': ['native_object_fixture::*Item']}})
        inventory = native_lifetimes.inspect(object_policy, ROOT / 'tests/fixtures',
            [Path('native-objects.cpp')], work / 'object-inventory')
        selection = Policy.parse({'schema_version': 3, 'languages': ['c', 'cpp'],
                                  'functions': {'include': ['selected*', 'object_fixture_*']}})
        environment.update({key: value for key, value in llvm_backend.environment(selection, 'cpp').items()
                            if key.startswith('OTELC_')})
        environment.update(OTELC_SOURCE_ROOT=str(ROOT / 'tests/fixtures'),
            OTELC_SOURCE_INCLUDE='["**"]', OTELC_SOURCE_EXCLUDE='[]')
        environment['LLVM_PROFILE_FILE'] = str(work / '%m-%p.profraw')
        def run(arguments: list[str]) -> None:
            commands.append(arguments)
            subprocess.run(arguments, cwd=ROOT, env=environment, check=True, timeout=600)
        run([cmake, '-S', str(NATIVE), '-B', str(work), '-G', 'Ninja',
             '-DCMAKE_MAKE_PROGRAM=' + str(tools[1]), '-DCMAKE_BUILD_TYPE=Release',
             '-DCMAKE_CXX_COMPILER=' + str(binaries / 'clang++'),
             '-DCMAKE_C_COMPILER=' + str(binaries / 'clang'), '-DOTELC_NATIVE_COVERAGE=ON',
             '-DOTELC_FUNCTION_PLUGIN=' + manifest['plugin']['path'],
             '-DOTELC_OBJECT_PLUGIN=' + object_manifest['plugin']['path'],
             '-DOTELC_OBJECT_INVENTORY=' + str(work / 'object-inventory/native-lifetimes.json'),
             '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON'])
        run([cmake, '--build', str(work), '--target', 'otelc_native_controls_test',
             'otelc_native_functions_test', 'otelc_native_objects_test', '--parallel', '2'])
        run([str(ctest), '--test-dir', str(work), '--output-on-failure'])
        object_refusals(work, inventory, object_manifest, binaries, environment)
        profiles = sorted(path for path in work.glob('*.profraw')
                          if not path.name.startswith('object-compiler-'))
        if not profiles:
            raise ValueError('native metric execution produced no coverage profiles')
        merged = work / 'control.profdata'
        run([str(binaries / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles), '-o', str(merged)])
        executable = work / 'otelc_native_controls_test'
        functions = work / 'otelc_native_functions_test'
        objects = work / 'otelc_native_objects_test'
        # The library is compiled once for all three tests. The object image
        # includes every owned implementation; use that map once with the
        # merged app counters, separately from the compiler plugin counters.
        command = [str(binaries / 'llvm-cov'), 'export', str(objects), '-instr-profile=' + str(merged),
                   *map(str, sorted(expected))]
        commands.append(command)
        coverage = json.loads(subprocess.check_output(command, env=environment, timeout=60))
        lines = summaries(coverage, expected)
        lcov = output / 'native-control.lcov'
        lcov.write_bytes(subprocess.check_output([*command, '-format=lcov'], env=environment, timeout=60))
        database = output / 'native-sdk-compile-commands.json'
        shutil.copyfile(work / 'compile_commands.json', database)
        if before != [llvm_backend.identity(path) for path in sources] or tool_inputs != [llvm_backend.identity(path) for path in tools] or not llvm_backend.unchanged(llvm):
            raise ValueError('native metric source or tooling changed during validation')
        shutil.copyfile(executable, output / 'native-control-test')
        shutil.copyfile(functions, output / 'native-function-test')
        shutil.copyfile(merged, output / 'native-control.profdata')
        shutil.copyfile(objects, output / 'native-object-test')
        shutil.copyfile(work / 'object-refusals.json', output / 'native-object-refusals.json')
        object_report(output, work, object_plugin, object_manifest, binaries, environment)
        for row in inventory['inputs']:
            if native_lifetimes.native_gate.identity(Path(row['path'])) != row:
                raise ValueError('native object fixture inputs changed during checks')
        llvm_backend.verify(plugin)
        (output / 'native-control-coverage.json').write_text(json.dumps({
            'source_inputs': before, 'tool_inputs': tool_inputs, 'llvm': llvm,
            'product': llvm_backend.identity(output / 'native-control-test'),
            'function_product': llvm_backend.identity(output / 'native-function-test'),
            'function_plugin': manifest, 'object_plugin': object_manifest,
            'object_product': llvm_backend.identity(output / 'native-object-test'),
            'lcov': llvm_backend.identity(lcov), 'compilation_database': llvm_backend.identity(database),
            'line_coverage': lines, 'commands': commands,
            'scope': 'SDK controls/function/object engines with unchanged internal compiler-linked fixtures; no public bootstrap or downstream OTLP qualification'}, indent=2) + '\n')
        print('Native metric control line coverage:', lines)


if __name__ == '__main__':
    check()
