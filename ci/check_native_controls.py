"""Build real C++ SDK controls and enforce their own LLVM coverage floor."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from otelc import llvm_backend
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
               llvm_backend.HEADER, Path(__file__).resolve()]
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
        selection = Policy.parse({'schema_version': 3, 'languages': ['c', 'cpp'],
                                  'functions': {'include': ['selected*']}})
        environment.update({key: value for key, value in llvm_backend.environment(selection, 'cpp').items()
                            if key.startswith('OTELC_')})
        environment['LLVM_PROFILE_FILE'] = str(work / '%m-%p.profraw')
        def run(arguments: list[str]) -> None:
            commands.append(arguments)
            subprocess.run(arguments, cwd=ROOT, env=environment, check=True, timeout=600)
        run([cmake, '-S', str(NATIVE), '-B', str(work), '-G', 'Ninja',
             '-DCMAKE_MAKE_PROGRAM=' + str(tools[1]), '-DCMAKE_BUILD_TYPE=Release',
             '-DCMAKE_CXX_COMPILER=' + str(binaries / 'clang++'),
             '-DCMAKE_C_COMPILER=' + str(binaries / 'clang'), '-DOTELC_NATIVE_COVERAGE=ON',
             '-DOTELC_FUNCTION_PLUGIN=' + manifest['plugin']['path'],
             '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON'])
        run([cmake, '--build', str(work), '--target', 'otelc_native_controls_test',
             'otelc_native_functions_test', '--parallel', '2'])
        run([str(ctest), '--test-dir', str(work), '--output-on-failure'])
        profiles = sorted(work.glob('*.profraw'))
        if not profiles:
            raise ValueError('native metric execution produced no coverage profiles')
        merged = work / 'control.profdata'
        run([str(binaries / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles), '-o', str(merged)])
        executable = work / 'otelc_native_controls_test'
        functions = work / 'otelc_native_functions_test'
        # Both tests link the same profiled control objects. Use the function
        # image's complete map once with both executions' merged counters; adding
        # the control image duplicates unused inline-header mappings (hash zero).
        command = [str(binaries / 'llvm-cov'), 'export', str(functions), '-instr-profile=' + str(merged),
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
        llvm_backend.verify(plugin)
        (output / 'native-control-coverage.json').write_text(json.dumps({
            'source_inputs': before, 'tool_inputs': tool_inputs, 'llvm': llvm,
            'product': llvm_backend.identity(output / 'native-control-test'),
            'function_product': llvm_backend.identity(output / 'native-function-test'),
            'function_plugin': manifest,
            'lcov': llvm_backend.identity(lcov), 'compilation_database': llvm_backend.identity(database),
            'line_coverage': lines, 'commands': commands,
            'scope': 'SDK controls/function engine with internal compiler-linked fixtures; no public bootstrap or downstream OTLP qualification'}, indent=2) + '\n')
        print('Native metric control line coverage:', lines)


if __name__ == '__main__':
    check()
