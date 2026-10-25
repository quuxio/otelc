"""Build the real native HTTP SDK and gate launcher execution/coverage/context."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from ci.check_native_controls import summaries
from ci.native_sonar import compilation_database
from otelc import llvm_backend, native_launch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'adapters/native/launcher/src/runtime.cpp'


def test_result(value: Any) -> None:
    """Do not accept successful unittest exit with a skipped native integration group."""
    if (type(value) is not dict or type(value.get('selected')) is not int or value['selected'] < 8
            or value.get('tests_run') != value['selected'] or value.get('skipped') != 0
            or value.get('failures') != 0 or value.get('errors') != 0 or value.get('successful') is not True):
        raise ValueError('native launch tests were missing, skipped or unsuccessful')


def check() -> None:
    """Keep actual dependency headers for CFamily; otherwise retain compact proof only."""
    output = ROOT / 'build'
    output.mkdir(exist_ok=True)
    sources = [*sorted((ROOT / 'adapters/native').rglob('*.cpp')),
               *sorted((ROOT / 'adapters/native').rglob('*.h')),
               ROOT / 'adapters/native/CMakeLists.txt', ROOT / 'adapters/native/launcher/CMakeLists.txt',
               llvm_backend.HEADER, llvm_backend.SOURCE, ROOT / 'native/llvm/object_abi.h',
               *(ROOT / path for path in ('otelc/native_launch.py', 'otelc/llvm_backend.py',
                  'otelc/config.py', 'otelc/file_inputs.py', 'otelc/control.py', 'otelc/health.py',
                  'tests/test_native_launch.py', 'tests/test_native_launch_tooling.py',
                  'tests/test_native_object_launch_runtime.py',
                  'tests/test_java_integration.py', 'ci/native_launch_suite.py', 'ci/check_native_launch.py'))]
    before = [llvm_backend.identity(path) for path in sources]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('OTEL', 'LLVM_PROFILE'))
                   and key not in ('CXXFLAGS', 'CFLAGS', 'CPPFLAGS', 'LDFLAGS')}
    commands = []
    keep_analysis = os.environ.get('OTELC_CPP_ANALYSIS') == '1'
    with tempfile.TemporaryDirectory(prefix='native-launch-', dir=output, delete=not keep_analysis) as temporary:
        work = Path(temporary).resolve()
        sdk = native_launch.build_sdk(work / 'sdk', coverage=True)
        library = Path(sdk['library']['path'])
        tools = Path(sdk['toolchain']['directory'])
        tool_inputs = [llvm_backend.identity(tools / name) for name in ('llvm-profdata', 'llvm-cov')]
        report = work / 'tests.json'
        environment.update(OTELC_NATIVE_LIBRARY=str(library), OTELC_NATIVE_TEST_REPORT=str(report),
                           LLVM_PROFILE_FILE=str(work / 'runtime-%p.profraw'),
                           PYTHONPATH=str(ROOT))
        command = [sys.executable, '-m', 'coverage', 'run',
                   '--data-file=' + str(output / 'native-launch-python.coverage'),
                   '--source=otelc.native_launch,ci.native_launch_suite', '-m', 'ci.native_launch_suite']
        commands.append(command)
        subprocess.run(command, cwd=ROOT, env=environment, check=True, timeout=600)
        results = json.loads(report.read_text())
        test_result(results)
        profiles = sorted(work.glob('runtime-*.profraw'))
        if not profiles:
            raise ValueError('native launch execution produced no coverage profiles')
        merged = output / 'native-launch.profdata'
        command = [str(tools / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles), '-o', str(merged)]
        commands.append(command)
        subprocess.run(command, cwd=ROOT, env=environment, check=True, timeout=60)
        command = [str(tools / 'llvm-cov'), 'export', str(library), '-instr-profile=' + str(merged), str(SOURCE)]
        commands.append(command)
        raw = json.loads(subprocess.check_output(command, env=environment, timeout=60))
        lines = summaries(raw, {SOURCE})
        lcov = output / 'native-launch.lcov'
        lcov.write_bytes(subprocess.check_output([*command, '-format=lcov'], env=environment, timeout=60))
        database = output / 'native-launch-compile-commands.json'
        entries = compilation_database([library.parent / 'compile_commands.json'], {SOURCE})
        database.write_text(json.dumps(entries, indent=2) + '\n')
        if before != [llvm_backend.identity(path) for path in sources]:
            raise ValueError('native launch sources changed during validation')
        if not llvm_backend.unchanged(sdk['toolchain']) or tool_inputs != [llvm_backend.identity(tools / name) for name in ('llvm-profdata', 'llvm-cov')]:
            raise ValueError('native launch tooling changed during validation')
        if sdk['library'] != native_launch.identity(library):
            raise ValueError('native launch SDK product changed during validation')
        product = output / library.name
        shutil.copyfile(library, product)
        (output / 'native-launch-coverage.json').write_text(json.dumps({
            'source_inputs': before, 'tool_inputs': tool_inputs, 'sdk_build': sdk,
            'product': llvm_backend.identity(product), 'tests': results,
            'lcov': llvm_backend.identity(lcov), 'line_coverage': lines,
            'compilation_database': llvm_backend.identity(database), 'commands': commands,
            'scope': 'Actual unchanged C/C++ functions, live controls and SDK HTTP; not full language admission'}, indent=2) + '\n')
        print('Native launch runtime line coverage:', lines)


if __name__ == '__main__':
    check()
