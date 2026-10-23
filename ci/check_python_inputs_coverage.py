"""Profile real CPython entry execution and gate every native component file."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ci import build_python_inputs as tooling

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SOURCES = ('CodeCapture.c', 'EntryCapture.c', 'ImportCapture.c', 'InputJournal.c', 'NativeMain.c', 'NativeEncoder.c', 'FileIOCapture.c')


def summaries(data: dict) -> dict:
    """Coverage for another file or an empty component cannot satisfy this gate."""
    result = {}
    for name in NATIVE_SOURCES:
        expected = ROOT / 'native/python' / name
        matches = [row['summary'] for entry in data['data'] for row in entry['files']
                   if Path(row['filename']).resolve() == expected]
        if len(matches) != 1:
            raise ValueError('native Python coverage must identify each actual source once: ' + name)
        summary = matches[0]
        if not summary['lines']['count'] or summary['lines']['percent'] < 80:
            raise ValueError('native Python line coverage is below80%: ' + name)
        result[name] = summary
    return result


def check() -> dict:
    """Exercise real native/FD/error cases, retaining per-file line and branch data."""
    binary = tooling.build(coverage=True)
    manifest = json.loads((binary.parent / 'build.json').read_text())
    llvm = Path(os.environ.get('OTELC_LLVM_BIN', str(Path(tooling.compiler()).parent)))
    with tempfile.TemporaryDirectory(prefix='python-native-coverage-', dir=ROOT / 'build') as temporary:
        directory = Path(temporary)
        environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'tests'),
                       'OTELC_NATIVE_COVERAGE': '1',
                       'LLVM_PROFILE_FILE': str(directory / 'inputs-%p-%m.profraw')}
        subprocess.run([sys.executable, '-m', 'unittest', 'test_python_native_inputs',
                        'test_python_import_inputs', 'test_python_code_inputs', 'test_python_native_encoder',
                        'test_python_origin_writer', 'test_python_fileio_inputs', '-v'],
                       cwd=ROOT, env=environment, check=True)
        profiles = list(directory.glob('*.profraw'))
        if not profiles:
            raise ValueError('actual native Python execution produced no LLVM profile')
        profile = directory / 'merged.profdata'
        subprocess.run([str(llvm / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles),
                        '-o', str(profile)], check=True)
        fixture = ROOT / 'build/native' / ('python-input-fixture-' + sys.implementation.cache_tag)
        imports = ROOT / 'build/native' / ('python-import-fixture-' + sys.implementation.cache_tag)
        encoder = ROOT / 'build/native' / ('python-encoder-fixture-' + sys.implementation.cache_tag)
        output = subprocess.check_output([str(llvm / 'llvm-cov'), 'export', str(binary),
            '-object', str(binary.parent / manifest['library']), '-object', str(fixture),
            '-object', str(imports), '-object', str(encoder),
            '-instr-profile=' + str(profile)], text=True)
        (ROOT / 'build/python-native-coverage.raw.json').write_text(output)
        result = {'interpreter': manifest['inputs']['interpreter'],
                  'version': sys.version, 'sources': summaries(json.loads(output))}
    (ROOT / 'build/python-native-coverage.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    print(json.dumps(check(), indent=2))
