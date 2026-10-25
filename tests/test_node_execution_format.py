"""Retain returned bytes separately from actual module-body entry and retries."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otelc import node, node_provenance
from otelc.config import Policy
from node_launch_evidence import run_checked
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class NodeExecutionFormatTests(unittest.TestCase):
    def test_public_protocol_descriptor_needs_no_node_or_application_launch(self) -> None:
        with mock.patch.object(node.subprocess, 'run', side_effect=AssertionError('unexpected Node launch')):
            self.assertEqual(node.protocols(), {'schema_version': 1, 'kind': 'otelc.node.protocols',
                'source': 5, 'execution': 1, 'request': 1, 'preload': 1, 'sdk': 1})

    def test_native_javascript_format_detection_and_known_module_without_import_syntax(self) -> None:
        cases = (('default', '.js', '', '', 'commonjs'),
                 ('detected', '.js', '', 'export {};\n', 'module'),
                 ('mjs', '.mjs', '', '', 'module'),
                 ('package-module', '.js', 'module', '', 'module'),
                 ('lexical', '.js', '', 'const module = 7;\n', 'module'))
        capture = Capture()
        self.addCleanup(capture.close)
        for label, extension, package, prefix, expected in cases:
            with self.subTest(profile=label), tempfile.TemporaryDirectory(dir='/tmp') as temporary:
                root = Path(temporary).resolve()
                if package:
                    (root / 'package.json').write_text(json.dumps({'type': package}))
                source = prefix + 'function workload(){return typeof require === "undefined" ? "module" : "commonjs";}\nconsole.log(workload());\n'
                entry = root / ('sample' + extension)
                entry.write_text(source)
                spans_before = sum(len(scope.spans) for request in capture.traces
                    for resource in request.resource_spans for scope in resource.scope_spans)
                baseline = subprocess.run([node.executable(), str(entry)], capture_output=True,
                                          text=True, timeout=15)
                self.assertEqual((baseline.returncode, baseline.stdout), (0, expected + '\n'), baseline.stderr)
                runtime, result = self.run_source(root, entry, capture, 'javascript', 'native', False)
                self.assertEqual((result.returncode, result.stdout), (0, baseline.stdout), result.stderr)
                report = json.loads((runtime / 'node-inputs.json').read_text())
                self.assertEqual(report['phase'], 'verified')
                entered = [item for item in report['loaded_inputs'] if item['kind'] == 'execution_started']
                self.assertEqual(len(entered), 1)
                self.assertEqual(entered[0]['format'], expected)
                self.assertEqual(entered[0]['pid'], report['application_pid'])
                spans_after = [span for request in capture.traces
                    for resource in request.resource_spans for scope in resource.scope_spans for span in scope.spans]
                self.assertEqual(len(spans_after), spans_before + 1)
                self.assertEqual(spans_after[-1].name, 'sample.workload')
                outcomes = report['module_outcomes']
                self.assertEqual(len([item for item in outcomes if item['outcome'] == 'body_entered']), 1)
                self.assertTrue(all(item['outcome'] in ('body_entered', 'unentered_before_same_url_entry') for item in outcomes))
                self.assertEqual(entry.read_text(), source)
                self.check_negative_journals(runtime, report)

    def test_default_typescript_preserve_format_is_concrete_in_both_actual_compilers(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        for backend in ('native', 'api'):
            for plain in (True, False):
                with self.subTest(backend=backend, plain=plain), tempfile.TemporaryDirectory(dir='/tmp') as temporary:
                    root = Path(temporary).resolve()
                    entry = root / 'sample.ts'
                    source = 'function workload(value: number){return typeof require === "undefined" ? "module" : "commonjs";}\nconsole.log(workload(7));\n'
                    entry.write_text(source)
                    runtime, result = self.run_source(root, entry, capture, 'typescript', backend, plain)
                    self.assertEqual((result.returncode, result.stdout), (0, 'module\n'), result.stderr)
                    prefix = 'node-plain' if plain else 'node'
                    report = json.loads((runtime / (prefix + '-inputs.json')).read_text())
                    self.assertEqual(report['phase'], 'verified')
                    formats = [item['format'] for item in report['loaded_inputs']
                               if item['kind'] in ('compiled', 'loaded', 'instrumented', 'execution_started')]
                    self.assertTrue(formats)
                    self.assertEqual(set(formats), {'module'})
                    self.assertEqual(len([item for item in report['module_outcomes']
                                          if item['outcome'] == 'body_entered']), 1)
                    self.assertTrue(all(item['outcome'] in ('body_entered', 'unentered_before_same_url_entry')
                                        for item in report['module_outcomes']))
                    self.assertEqual(entry.read_text(), source)

    def test_a_known_failed_witness_is_not_forgiven_by_later_same_url_entry(self) -> None:
        # Exercise the real recorder and generated observation helper without
        # claiming that this isolated protocol fixture is a complete launch.
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            entry = root / 'sample.js'
            entry.write_text('function workload(){return 7;}\n')
            policy = Policy.parse({'schema_version': 3, 'sources': {'include': ['**']}})
            runtime = root / 'runtime'
            node.command(policy, runtime, 'javascript', root=root, entry=entry, plain=True)
            manifest_file = runtime / 'node-plain-launch.json'
            recorder = ROOT / 'adapters/node/src/source-inputs.ts'
            transform = ROOT / 'adapters/node/src/compiler/transform.mjs'
            script = f'''import assert from 'node:assert/strict';
import {{readFileSync}} from 'node:fs';
import {{createRequire}} from 'node:module';
import {{pathToFileURL}} from 'node:url';
import {{compileFunction}} from 'node:vm';
import {{SourceInputs,moduleEntered}} from {json.dumps(recorder.as_uri())};
import {{observeModuleEntry}} from {json.dumps(transform.as_uri())};
const manifest=JSON.parse(readFileSync(process.argv[1],'utf8'));
const inputs=new SourceInputs(manifest.resolved_plan);
const file=manifest.entry.path,url=pathToFileURL(file).href;
function emit(){{
 const load=inputs.beginModule(file,'sample.js',url,'javascript');
 const source=inputs.source(file);
 const marked=observeModuleEntry(source,file,undefined,'commonjs',load.id);
 inputs.emitted(load,marked.code,'commonjs','loaded',marked.map,'commonjs');
 return {{load,marked}};
}}
const first=emit();
assert.throws(()=>moduleEntered(first.load.id,'module'),/does not match/);
const second=emit();
compileFunction(second.marked.code,['require'],{{filename:file}})(createRequire(url));
'''
            result = subprocess.run([node.executable(), '--input-type=module', '-e', script, str(manifest_file)],
                cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in (runtime / 'node-plain-sources.jsonl').read_text().splitlines()]
            self.assertEqual(len([item for item in records if item['kind'] == 'execution_started']), 1)
            self.assertFalse(any(item['kind'] == 'module_reentered' for item in records))
            with self.assertRaisesRegex(ValueError, 'entry is unverified'):
                node_provenance.verify_module_records(runtime, 'node-plain', records, json.loads(manifest_file.read_text()))

    def run_source(self, root, entry, capture, language, backend, plain):
        config = root / 'otelc.toml'
        config.write_text('schema_version=3\n[sources]\ninclude=["**"]\n'
            '[functions]\ninclude=["*.workload"]\n[traces]\nenabled=true\nsample_ratio=1.0\n'
            f'[export]\nendpoint="http://127.0.0.1:{capture.server.server_port}"\n')
        runtime = root / ('runtime-plain' if plain else 'runtime')
        result = run_checked([sys.executable, '-I', '-S', '-B', str(ROOT / 'otelc/cli_bootstrap.py'), '--config', str(config),
            'node', '--language', language, '--root', str(root), '--entry', str(entry),
            '--runtime', str(runtime), '--backend', backend, *(['--plain'] if plain else [])],
            cwd=ROOT, runtime=runtime, timeout=40)
        return runtime, result

    def check_negative_journals(self, runtime, report):
        journal = runtime / 'node-sources.jsonl'
        original = journal.read_bytes()
        manifest = json.loads((runtime / 'node-launch.json').read_text())
        records = report['loaded_inputs']
        cases = [('missing entry', [item for item in copy.deepcopy(records) if item['kind'] != 'execution_started'])]
        forged = copy.deepcopy(records)
        entry = next(item for item in forged if item['kind'] == 'execution_started')
        entry['format'] = 'module' if entry['format'] == 'commonjs' else 'commonjs'
        cases.append(('changed execution format', forged))
        forged = copy.deepcopy(records)
        forged.append(copy.deepcopy(next(item for item in forged if item['kind'] == 'execution_started')))
        cases.append(('duplicate body entry', forged))
        for field, value in (('pid', True), ('execution_protocol', 2),
                             ('emission_sha256', '0' * 64), ('source_sha256', '0' * 64)):
            forged = copy.deepcopy(records)
            next(item for item in forged if item['kind'] == 'execution_started')[field] = value
            cases.append(('changed ' + field, forged))
        if any(item['kind'] == 'module_reentered' for item in records):
            cases.append(('missing reentry', [item for item in copy.deepcopy(records) if item['kind'] != 'module_reentered']))
            forged = copy.deepcopy(records)
            next(item for item in forged if item['kind'] == 'instrumented')['body_entry']['schema_version'] = 99
            cases.append(('invalid unentered observer', forged))
        try:
            for label, values in cases:
                with self.subTest(negative=label):
                    journal.write_text(''.join(json.dumps(item) + '\n' for item in values))
                    with self.assertRaises((ValueError, KeyError)):
                        node_provenance.verify(runtime, manifest)
        finally:
            journal.write_bytes(original)

    def test_plain_ts_witness_preserves_hashbang_directives_rng_and_error_source_maps(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        source = '''#!/usr/bin/env node
"use strict";
function workload(value: number) { throw new Error("original"); }
let seed=7;Math.random=()=>((seed=(seed*1664525+1013904223)>>>0)/4294967296);
class Thing { static value=Math.random(); }
try { workload(7); } catch(error) {
 console.log(JSON.stringify({stack:error.stack.split("\\n").slice(0,2),value:Thing.value,
   random:Math.random(),name:Thing.name,keys:Reflect.ownKeys(Thing)}));
}
'''
        for backend in ('native', 'api'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory(dir='/tmp') as temporary:
                root = Path(temporary).resolve()
                entry = root / 'sample.cts'
                entry.write_text(source)
                # Use the actual Microsoft compiler as the baseline. Node's
                # builtin TS eraser has a different file-URL stack convention
                # on Node26; it is not either of these compiler profiles.
                compiler = ROOT / 'adapters/node/src/compiler/typescript.mjs'
                reference = root / 'reference.mjs'
                reference.write_text(f'''import {{registerHooks}} from 'node:module';
import {{readFileSync}} from 'node:fs';
import {{fileURLToPath}} from 'node:url';
import {{transpile}} from {json.dumps(compiler.as_uri())};
registerHooks({{load(url,context,nextLoad){{
 if(url !== {json.dumps(entry.as_uri())}) return nextLoad(url,context);
 const output=transpile(readFileSync(fileURLToPath(url),'utf8'),fileURLToPath(url),'sample.cts',
   {{plain:true,backend:{json.dumps(backend)}}},{json.dumps(str(root))});
 return {{format:output.format,shortCircuit:true,source:output.code+
   '\\n//# sourceMappingURL=data:application/json;base64,'+Buffer.from(JSON.stringify(output.map)).toString('base64')}};
}}}});
''')
                baseline = subprocess.run([node.executable(), '--enable-source-maps', '--import', str(reference), str(entry)],
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(baseline.returncode, 0, baseline.stderr)
                expected = json.loads(baseline.stdout)
                for plain in (True, False):
                    runtime, result = self.run_source(root, entry, capture, 'typescript', backend, plain)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), expected)
                    prefix = 'node-plain' if plain else 'node'
                    report = json.loads((runtime / (prefix + '-inputs.json')).read_text())
                    emission = next(item for item in report['loaded_inputs'] if item['kind'] == ('loaded' if plain else 'instrumented'))
                    self.assertTrue(Path(emission['output_file']).read_text().startswith('#!/usr/bin/env node'))
                    self.assertEqual(entry.read_text(), source)
