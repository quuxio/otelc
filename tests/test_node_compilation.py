"""Compiler evidence records consumed inputs rather than reconstructed intent."""

from __future__ import annotations

import hashlib
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from otelc import node, node_provenance
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parent.parent


class NodeCompilationTests(unittest.TestCase):
    def test_actual_input_options_and_raw_outputs_survive_both_compiler_profiles(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='otelc-compilation-', dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            entry = root / 'app.cts'
            original = 'function workload(value: number): number { return value + 1; }\nconsole.log(workload(7));\n'
            entry.write_text(original)
            (root / 'base.json').write_text('{"compilerOptions":{"target":"ES2022","strict":true}}\n')
            (root / 'tsconfig.json').write_text('{"extends":"./base.json"}\n')
            policy = root / 'otelc.toml'
            policy.write_text('schema_version = 3\n[sources]\ninclude = ["**"]\n'
                '[functions]\ninclude = ["*.workload"]\n[export]\n'
                f'endpoint = "http://127.0.0.1:{capture.server.server_port}"\n')
            for backend in ('native', 'api'):
                for plain in (True, False):
                    with self.subTest(backend=backend, plain=plain):
                        runtime = root / (backend + ('-plain' if plain else '-instrumented'))
                        result = subprocess.run([sys.executable, '-m', 'otelc', '--config', str(policy), 'node',
                            '--language', 'typescript', '--root', str(root), '--entry', str(entry),
                            '--runtime', str(runtime), '--backend', backend, *(['--plain'] if plain else [])],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
                        self.assertEqual((result.returncode, result.stdout), (0, '8\n'), result.stderr)
                        prefix = 'node-plain' if plain else 'node'
                        report = json.loads((runtime / (prefix + '-inputs.json')).read_text())
                        self.assertEqual(report['phase'], 'verified')
                        self.assertEqual(report['schema_version'], 3)
                        records = report['loaded_inputs']
                        attempts = [item for item in records if item['kind'] == 'module_requested']
                        self.assertEqual(len(attempts), 1)
                        compilations = [item for item in records if item['kind'] == 'compilation']
                        self.assertEqual(len(compilations), 1)
                        compilation = compilations[0]
                        self.assertEqual(compilation['load_id'], attempts[0]['load_id'])
                        evidence = compilation['compiler']
                        self.assertEqual(evidence['backend'], backend)
                        self.assertEqual(evidence['version'], '7.0.2' if backend == 'native' else '6.0.3')
                        files = evidence['files']
                        for artifact in files.values():
                            content = Path(artifact['file']).read_bytes()
                            self.assertEqual(len(content), artifact['size'])
                            self.assertEqual(hashlib.sha256(content).hexdigest(), artifact['sha256'])
                        compiler_input = Path(files['input']['file']).read_text()
                        if backend == 'native' and not plain:
                            self.assertIn('@quux.otelc.identity', compiler_input)
                            self.assertNotEqual(compiler_input, original)
                        else:
                            self.assertEqual(compiler_input, original)
                        options = json.loads(Path(files['options']['file']).read_text())
                        if backend == 'native':
                            self.assertEqual(options['compilerOptions']['module'], 'node16')
                            self.assertEqual(options['files'], [files['input']['path']])
                            self.assertFalse(Path(files['input']['path']).exists())
                            self.assertIn('preparation_map', files)
                        else:
                            self.assertEqual(options['module'], 1)
                        self.assertIn('sourceMappingURL=', Path(files['output']['file']).read_text())
                        self.assertEqual(entry.read_text(), original)
                        if not plain:
                            self.check_forged_evidence(runtime, prefix, report['loaded_inputs'], backend)

    def check_forged_evidence(self, runtime: Path, prefix: str, records: list[dict], backend: str) -> None:
        journal = runtime / (prefix + '-sources.jsonl')
        original = journal.read_bytes()
        manifest = json.loads((runtime / (prefix + '-launch.json')).read_text())
        cases = []
        cases.append(('missing requests', [item for item in copy.deepcopy(records) if item['kind'] != 'module_requested']))
        cases.append(('missing compiler', [item for item in copy.deepcopy(records) if item['kind'] != 'compilation']))
        changed = copy.deepcopy(records)
        compiler = next(item['compiler'] for item in changed if item['kind'] == 'compilation')
        compiler['compiler_path'] = '/untrusted/compiler'
        cases.append(('substituted compiler', changed))
        changed = copy.deepcopy(records)
        next(item for item in changed if item['kind'] == 'module_requested')['url'] = 'file:///untrusted/origin.cts'
        cases.append(('substituted URL', changed))
        changed = copy.deepcopy(records)
        next(item for item in changed if item['kind'] == 'compiled')['load_id'] = 999
        cases.append(('unrequested phase', changed))
        changed = copy.deepcopy(records)
        changed.append(copy.deepcopy(next(item for item in changed if item['kind'] == 'module_requested')))
        cases.append(('duplicate request', changed))
        changed = copy.deepcopy(records)
        compiler = next(item['compiler'] for item in changed if item['kind'] == 'compilation')
        role = 'options'
        options = json.loads(Path(compiler['files'][role]['file']).read_text())
        if backend == 'api':
            options['module'] = 200  # Valid bytes/metadata, wrong execution format.
        else:
            options['files'] = ['/untrusted/input.cts']
        content = json.dumps(options).encode()
        digest = hashlib.sha256(content).hexdigest()
        filename = runtime / (prefix + '-module-' + digest + '.txt')
        filename.write_bytes(content)
        filename.chmod(0o600)
        compiler['files'][role].update(file=str(filename), sha256=digest, size=len(content))
        cases.append(('rehashed wrong options', changed))
        try:
            for label, changed in cases:
                with self.subTest(negative=label, backend=backend):
                    journal.write_text(''.join(json.dumps(item) + '\n' for item in changed))
                    with self.assertRaises((ValueError, KeyError)):
                        node_provenance.verify(runtime, manifest)
        finally:
            journal.write_bytes(original)

    def test_mutated_native_request_is_refused_after_the_actual_compiler_runs(self) -> None:
        script = '''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import ts from 'typescript-api';
import { nativeEmit } from './adapters/node/src/compiler/typescript-native.mjs';
const target = process.argv[1];
let directory;
const run = (binary, args, options) => {
  const config = args[1];
  const request = JSON.parse(fs.readFileSync(config, 'utf8'));
  directory = path.dirname(config);
  const result = spawnSync(binary, args, options);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  if (target === 'fifo') {
    fs.unlinkSync(request.files[0]);
    assert.equal(spawnSync('mkfifo', [request.files[0]]).status, 0);
  } else fs.appendFileSync(target === 'input' ? request.files[0] : config, '\\n// changed after compiler read\\n');
  return result;
};
assert.throws(() => nativeEmit('export const value = 7;', '/ordinary/app.cts', 'app.cts',
  {plain:true}, {target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,sourceMap:true}, run),
  /compiler input or configuration changed|not a regular file/);
assert.equal(fs.existsSync(directory), false);
'''
        for target in ('input', 'config', 'fifo'):
            with self.subTest(target=target):
                result = subprocess.run([node.executable(), '--input-type=module', '-e', script, target],
                    cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_package_type_cannot_change_requested_commonjs_emission(self) -> None:
        script = '''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import ts from 'typescript-api';
import { nativeEmit } from './adapters/node/src/compiler/typescript-native.mjs';
const run = (binary, args, options) => {
  fs.writeFileSync(path.join(path.dirname(args[1]), 'package.json'), '{"type":"module"}');
  return spawnSync(binary, args, options);
};
const result = nativeEmit('export function workload(){ return 7; }', '/ordinary/app.ts', 'app.ts',
  {plain:true}, {target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,sourceMap:true}, run);
assert.match(result.code, /exports.workload/);
assert.equal(result.compilation.files.input.path.endsWith('.cts'), true);
'''
        result = subprocess.run([node.executable(), '--input-type=module', '-e', script],
            cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_caught_loader_refusal_cannot_be_verified_as_a_completed_module(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='otelc-caught-load-', dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            entry = root / 'entry.mjs'
            entry.write_text("await import('./refused.mts').catch(() => console.log('caught'));\n")
            (root / 'refused.mts').write_text('export async function* workload() { yield 1; }\n')
            policy = root / 'otelc.toml'
            policy.write_text('schema_version = 3\n[sources]\ninclude = ["**"]\n'
                '[functions]\ninclude = ["*.workload"]\n[traces]\nenabled = true\n'
                '[export]\n' + f'endpoint = "http://127.0.0.1:{capture.server.server_port}"\n')
            runtime = root / 'runtime'
            result = subprocess.run([sys.executable, '-m', 'otelc', '--config', str(policy), 'node',
                '--language', 'javascript', '--backend', 'api', '--root', str(root), '--entry', str(entry),
                '--runtime', str(runtime)], cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.stdout, 'caught\n')
            self.assertEqual(result.returncode, 1)
            report = json.loads((runtime / 'node-inputs.json').read_text())
            self.assertEqual(report['phase'], 'failed')
            self.assertIn('module load did not finish', report['error'])
