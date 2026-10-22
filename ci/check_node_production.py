"""Exercise both Microsoft compiler profiles from an isolated production install."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from otelc.node import executable

ROOT = Path(__file__).resolve().parent.parent
COMPILER_CHECK = """
import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript-api';
import { transpile } from './adapters/node/src/compiler/typescript.mjs';
import { nativeVersion } from './adapters/node/src/compiler/typescript-native.mjs';
const filename = process.argv[1];
const source = fs.readFileSync(filename, 'utf8');
const results = {};
for (const backend of ['native', 'api']) {
  const output = transpile(source, filename, 'app.ts', {
    plain: true, backend, lifetimes: { enabled: false }
  }, path.dirname(filename), 'module');
  const application = await import('data:text/javascript;base64,' + Buffer.from(output.code).toString('base64') + '#' + backend);
  results[backend] = application.workload(7);
}
console.log(JSON.stringify({ native_version: nativeVersion(), api_version: ts.version, results }));
"""


def check() -> dict[str, object]:
    """Use the lockfile's production closure, without writing application inputs."""
    selected = executable()
    environment = {**os.environ, 'PATH': str(Path(selected).parent) + os.pathsep + os.environ.get('PATH', '')}
    # This is a packaging check, not a second copy of adapter source coverage.
    environment.pop('NODE_V8_COVERAGE', None)
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='otelc-production-', dir='/tmp') as temporary:
        directory = Path(temporary).resolve()
        provider = directory / 'provider'
        provider.mkdir()
        for filename in ('package.json', 'package-lock.json'):
            shutil.copyfile(ROOT / filename, provider / filename)
        shutil.copytree(ROOT / 'adapters/node/src', provider / 'adapters/node/src')
        subprocess.run(['npm', 'ci', '--omit=dev', '--ignore-scripts', '--prefer-offline', '--no-audit', '--no-fund',
                        '--cache', str(build / 'npm-cache')], cwd=provider, env=environment,
                       check=True, capture_output=True, text=True, timeout=120)
        application = directory / 'application'
        application.mkdir()
        entry = application / 'app.ts'
        source = 'export function workload(value: number): number { return (value + 1) * 7; }\n'
        entry.write_text(source)
        original_bytes = entry.read_bytes()
        result = subprocess.run([selected, '--input-type=module', '-e', COMPILER_CHECK, str(entry)],
                                cwd=provider, env=environment, check=True, capture_output=True,
                                text=True, timeout=60)
        report = json.loads(result.stdout)
        if (report != {'native_version': '7.0.2', 'api_version': '6.0.3', 'results': {'native': 56, 'api': 56}}
                or entry.read_bytes() != original_bytes):
            raise ValueError('production compiler identity, output or source preservation check failed')
        return report


if __name__ == '__main__':
    print(json.dumps(check(), sort_keys=True))
