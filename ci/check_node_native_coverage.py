"""Enforce the native Promise observer's own LLVM line-coverage floor."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check() -> None:
    """Profile actual Node/worker execution, then restore the ordinary binary atomically."""
    node = os.environ.get('OTELC_NODE') or shutil.which('node')
    if not node:
        raise ValueError('Node is required for native coverage')
    llvm = Path(os.environ.get('OTELC_LLVM_BIN', '/opt/homebrew/opt/llvm@22/bin' if sys.platform == 'darwin'
                               else str(Path(shutil.which('clang++') or '/usr/bin/clang++').parent)))
    probe_environment = {key: value for key, value in os.environ.items() if key not in ('NODE_OPTIONS', 'NODE_PATH')}
    version = subprocess.check_output([node, '-p', 'process.versions.node'], text=True, env=probe_environment).strip()
    library = ROOT / 'build/tools' / f'otelc_node_observer-{version}.node'
    if not library.is_file():
        raise ValueError('build the ordinary Node observer before profiling it')
    with tempfile.TemporaryDirectory(prefix='node-native-coverage-', dir=ROOT / 'build') as temporary:
        directory = Path(temporary)
        backup = directory / 'ordinary.node'
        shutil.copy2(library, backup)
        environment = {**probe_environment, 'OTELC_NODE': node, 'OTELC_NATIVE_COVERAGE': '1',
                       'LLVM_PROFILE_FILE': str(directory / 'observer-%p-%m.profraw')}
        try:
            subprocess.run([sys.executable, 'ci/build_node_observer.py'], cwd=ROOT, env=environment, check=True)
            subprocess.run([node, '--test', 'adapters/node/test/promise-native.test.ts'], cwd=ROOT,
                           env=environment, check=True)
            profiles = list(directory.glob('*.profraw'))
            if not profiles:
                raise ValueError('actual native observer execution produced no LLVM profile')
            profile = directory / 'merged.profdata'
            subprocess.run([str(llvm / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles), '-o', str(profile)], check=True)
            output = subprocess.check_output([str(llvm / 'llvm-cov'), 'export', str(library),
                                              '-instr-profile=' + str(profile)], text=True)
            data = json.loads(output)
            files = [item for entry in data['data'] for item in entry['files']
                     if Path(item['filename']).resolve() == ROOT / 'native/node/PromiseObserver.cpp']
            if len(files) != 1:
                raise ValueError('native coverage must identify the actual observer source exactly once')
            summary = files[0]['summary']
            (ROOT / 'build/node-native-coverage.json').write_text(json.dumps({
                'node': version, 'source': 'native/node/PromiseObserver.cpp', 'summary': summary}, indent=2) + '\n')
            if summary['lines']['count'] == 0 or summary['lines']['percent'] < 80:
                raise ValueError('native Promise observer line coverage is below 80%')
            print(f"Native Node observer line coverage: {summary['lines']['percent']:.2f}%")
        finally:
            os.replace(backup, library)


if __name__ == '__main__':
    check()
