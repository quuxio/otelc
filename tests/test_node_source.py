"""Unedited ordinary applications produce decoded telemetry through the common policy."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from otelc import control, node
from otelc.benchmark import checksum
from otelc.config import Policy
from test_java_integration import Capture, batch, wait_for_count

ROOT = Path(__file__).resolve().parent.parent


def policy_for(capture: Capture) -> Policy:
    """One identical policy is applied to both language examples and compiler profiles."""
    data = copy.deepcopy(Policy.load(ROOT / 'examples/otelc.toml').data)
    data['metrics'].update(collection=False, emission=False)
    data['traces']['sample_ratio'] = 1.0
    data['export'].update(endpoint=f'http://127.0.0.1:{capture.server.server_port}', interval_ms=60000, timeout_ms=2000)
    return Policy.parse(data)


class NodeSourceTests(unittest.TestCase):
    def test_lifetime_body_functions_keep_constructor_and_release_lexical_bindings(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(policy_for(capture).data)
        data['sources'] = {'include': ['**'], 'exclude': []}
        data['functions'] = {'include': [], 'exclude': []}
        data['lifetimes']['include'] = ['*Item']
        data['traces']['enabled'] = False
        data['metrics']['collection'] = True
        policy = Policy.parse(data)
        body = """function read() { return [constant, mutable, Local.value]; }
var read;
function early() { return before; }
try { early(); } catch (error) { this.tdz = error instanceof ReferenceError; }
const before = 11;
this.tdz = this.tdz && early() === 11;
const constant = 7;
let mutable = 8;
class Local { static value = 9; }
const first = read();
mutable = 10;
this.snapshot = [first, read(), read.name];
this.later = read;
"""
        expected = {'snapshot': [[7, 8, 9], [7, 10, 9], 'read'], 'later': [7, 10, 9], 'tdz': True, 'void_close': True}
        for language, backend, extension in [('javascript', 'native', 'cjs'),
                                             ('typescript', 'native', 'ts'), ('typescript', 'api', 'cts')]:
            for site in ('constructor', 'release'):
                with self.subTest(language=language, backend=backend, site=site), tempfile.TemporaryDirectory(dir='/tmp') as temporary:
                    root = Path(temporary).resolve()
                    entry = root / ('app.' + extension)
                    constructor = body if site == 'constructor' else ''
                    release = body if site == 'release' else ''
                    entry.write_text('class Item { constructor() {\n' + constructor + '\n} close() {\n' + release + '\n} }\n'
                        'const item = new Item(); const result = item.close();\n'
                        'console.log(JSON.stringify({ snapshot: item.snapshot, later: item.later(), tdz: item.tdz, void_close: result === undefined }));\n')
                    original = entry.read_bytes()
                    for plain in (True, False):
                        runtime = root / ('plain' if plain else 'instrumented')
                        argv = node.command(policy, runtime, language, root=root, entry=entry, backend=backend, plain=plain)
                        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
                        if not plain:
                            report = node.wait_report(runtime)
                            self.assertEqual(report['phase'], 'closed')
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout), expected)
                        self.assertEqual(entry.read_bytes(), original)
                        if not plain:
                            self.assertEqual(report['lifetimes']['admitted'], 1)
                            self.assertEqual(report['lifetimes']['resource_completed'], 1)
                            self.assertEqual(report['lifetimes']['failed_releases'], 0)
                            self.assertEqual(report['lifetime_lost'], 0)

    def test_async_metrics_can_enable_from_startup_off_with_tracing_also_disabled(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(policy_for(capture).data)
        data['traces']['enabled'] = False
        data['lifetimes']['enabled'] = False
        data['sources'] = {'include': ['**'], 'exclude': []}
        policy = Policy.parse(data)
        with tempfile.TemporaryDirectory(prefix='otelc-all-off-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            entry = root / 'app.cjs'
            entry.write_text("""const input = require('node:readline').createInterface({input: process.stdin});
async function workload(value) { return await Promise.resolve(value); }
input.on('line', async line => {
  if (line === 'quit') { input.close(); return; }
  console.log(JSON.stringify({value: await workload(7)}));
});
""")
            runtime = root / 'instrumented'
            with subprocess.Popen(node.command(policy, runtime, 'javascript', root=root, entry=entry),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                try:
                    endpoint = runtime / 'control.json'
                    control.wait_ready(endpoint, process.pid)
                    self.assertEqual(batch(process, 'run'), {'value': 7})
                    control.request(endpoint, 'flush')
                    self.assertEqual(capture.metrics, [])
                    control.request(endpoint, 'on')
                    self.assertEqual(batch(process, 'run'), {'value': 7})
                    control.request(endpoint, 'flush')
                    self.assertEqual(wait_for_count(capture.calls, 1), 1)
                    stdout, stderr = process.communicate('quit\n', timeout=10)
                    self.assertEqual((process.returncode, stdout, stderr), (0, '', ''))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            report = node.wait_report(runtime)
            self.assertEqual(report['phase'], 'closed')
            self.assertEqual(report['function_completions'], 1)
            self.assertEqual(report['span_accounting']['received'], 0)
            self.assertEqual(capture.traces, [])

    def exercise_release_keys(self, language: str, backend: str) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(policy_for(capture).data)
        data['sources'] = {'include': ['**'], 'exclude': []}
        data['functions'] = {'include': [], 'exclude': []}
        data['lifetimes']['include'] = ['*']
        data['metrics'].update(collection=True, emission=True)
        data['traces']['sample_ratio'] = 1.0
        source = """let conversions = 0;
const close = 'other';
const key = { [Symbol.toPrimitive](hint) { conversions++; return 'close'; } };
class WorkItemAlias { [close]() {} }
class WorkItemComputed { [key]() { var WorkItemComputed = 7; globalThis.business = WorkItemComputed; } }
class WorkItemSymbol { [Symbol('close')]() {} }
class WorkItemOverridden { close() {} [close]() {} }
let failing = false;
const marker = new Error('original');
async function factory(name) {
  return class WorkItemFactory { [await Promise.resolve(name)]() { if (failing) throw marker; } };
}
const [Release, Other] = await Promise.all([factory('close'), factory('other')]);
const alias = new WorkItemAlias(); alias.other();
const computed = new WorkItemComputed(); computed.close(); computed.close();
const symbol = new WorkItemSymbol(); symbol[Reflect.ownKeys(WorkItemSymbol.prototype)[1]]();
const overridden = new WorkItemOverridden(); overridden.other();
const released = new Release();
failing = true;
try { Other.prototype.other.call(released); } catch (error) { if (error !== marker) throw error; }
failing = false;
released.close();
const other = new Other(); other.other();
const loops = [];
let iteration = 0;
while (loops.push(class WorkItemLoop { [iteration++ ? 'other' : 'close']() {} }) < 2) {}
const loopRelease = new loops[0](); loopRelease.close();
const loopOther = new loops[1](); loopOther.other();
const anonymous = [];
for (const key of ['close', 'other']) anonymous.push(class { [key]() {} });
const anonymousRelease = new anonymous[0](); anonymousRelease.close();
const anonymousOther = new anonymous[1](); anonymousOther.other();
globalThis.keep = [alias, computed, symbol, overridden, released, other, loopRelease, loopOther, anonymousRelease, anonymousOther];
console.log(JSON.stringify({conversions, business: globalThis.business}));
"""
        with tempfile.TemporaryDirectory(prefix='otelc-release-keys-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            entry = root / ('app.mts' if language == 'typescript' else 'app.mjs')
            entry.write_text(source)
            plain = subprocess.run(node.command(Policy.parse(data), root / 'plain', language,
                plain=True, root=root, entry=entry, backend=backend), capture_output=True, text=True, timeout=15, check=True)
            self.assertEqual((plain.stdout, plain.stderr), ('{"conversions":1,"business":7}\n', ''))
            runtime = root / 'instrumented'
            instrumented = subprocess.run(node.command(Policy.parse(data), runtime, language,
                root=root, entry=entry, backend=backend), capture_output=True, text=True, timeout=15)
            self.assertEqual(instrumented.returncode, 0, instrumented.stderr)
            self.assertEqual((instrumented.stdout, instrumented.stderr), (plain.stdout, plain.stderr))
            report = node.wait_report(runtime)
            self.assertEqual(report['phase'], 'closed')
            self.assertEqual(report['lifetimes']['resource_completed'], 4)
            self.assertEqual(report['lifetimes']['open_resources'], 1)
            self.assertEqual(report['lifetimes']['admitted'], 10)
            self.assertEqual(report['lifetime_lost'], 0)
            self.assertEqual(report['lifetimes']['failed_releases'], 0)
            spans = [span for message in capture.traces for resource in message.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), 4)
            self.assertTrue({'app.WorkItemComputed lifetime', 'app.WorkItemLoop lifetime',
                'app.factory.WorkItemFactory lifetime'}.issubset({span.name for span in spans}))
            self.assertTrue(all(dict((a.key, a.value.string_value) for a in span.attributes)['boundary']
                == 'resource' for span in spans))
            self.assertEqual(entry.read_text(), source)

    def test_javascript_computed_release_keys_and_class_identity(self) -> None:
        self.exercise_release_keys('javascript', 'native')

    def test_microsoft_native_computed_release_keys_and_class_identity(self) -> None:
        self.exercise_release_keys('typescript', 'native')

    def test_microsoft_api_computed_release_keys_and_class_identity(self) -> None:
        self.exercise_release_keys('typescript', 'api')

    def test_dynamic_unsupported_releases_report_loss_without_running_getters(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(policy_for(capture).data)
        data['sources'] = {'include': ['**'], 'exclude': []}
        data['functions'] = {'include': [], 'exclude': []}
        data['lifetimes']['include'] = ['*']
        data['metrics'].update(collection=True, emission=True)
        source = """const key = 'close';
const other = 'other';
let getters = 0;
let statics = 0;
class AsyncOther { async [other]() {} }
class AsyncRelease { async [key]() {} }
class FieldRelease { [key] = () => {}; }
class AccessorRelease { get [key]() { getters++; return () => {}; } }
class DuplicateRelease { ['close'](value) { throw new Error('overwritten'); } close() {} static { statics++; } }
class ShadowedRelease { close() { var ShadowedRelease = 7; const value = () => eval('ShadowedRelease'); if (value() !== 7) throw new Error('changed binding'); } }
const instances = [new AsyncOther(), new AsyncRelease(), new FieldRelease(), new AccessorRelease(), new DuplicateRelease(), new ShadowedRelease()];
await instances[0].other(); await instances[1].close(); instances[2].close(); instances[3].close(); instances[4].close(); instances[5].close();
globalThis.keep = instances;
console.log(JSON.stringify({getters, statics}));
"""
        with tempfile.TemporaryDirectory(prefix='otelc-release-shapes-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            entry = root / 'app.mjs'
            entry.write_text(source)
            policy = Policy.parse(data)
            plain = subprocess.run(node.command(policy, root / 'plain', 'javascript',
                plain=True, root=root, entry=entry), capture_output=True, text=True, timeout=15, check=True)
            runtime = root / 'instrumented'
            instrumented = subprocess.run(node.command(policy, runtime, 'javascript', root=root, entry=entry),
                capture_output=True, text=True, timeout=15)
            self.assertEqual(instrumented.returncode, 0, instrumented.stderr)
            self.assertEqual((instrumented.stdout, instrumented.stderr), (plain.stdout, plain.stderr))
            self.assertEqual(plain.stdout, '{"getters":1,"statics":1}\n')
            report = node.wait_report(runtime)
            self.assertEqual(report['lifetimes']['admitted'], 6)
            self.assertEqual(report['lifetimes']['resource_completed'], 1)
            self.assertEqual(report['lifetimes']['losses'], {'unsupported_release': 4})
            self.assertEqual(report['lifetime_lost'], 4)
            self.assertEqual(entry.read_text(), source)

    def test_unsafe_release_key_binding_scopes_are_refused_explicitly(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(policy_for(capture).data)
        data['sources'] = {'include': ['**'], 'exclude': []}
        data['functions'] = {'include': [], 'exclude': []}
        data['lifetimes']['include'] = ['*']
        cases = [
            ("function make(value = class WorkItem { close() {} }) { return value; } make();", 'parameter initialisers'),
            ("let count = 0; while (new (class { close() {} })() && count++ < 1) {}", 'loop heads'),
        ]
        with tempfile.TemporaryDirectory(prefix='otelc-release-scope-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            for index, (source, diagnostic) in enumerate(cases):
                with self.subTest(diagnostic=diagnostic):
                    entry = root / f'app{index}.mjs'
                    entry.write_text(source)
                    runtime = root / f'runtime{index}'
                    result = subprocess.run(node.command(Policy.parse(data), runtime, 'javascript', root=root, entry=entry),
                        capture_output=True, text=True, timeout=15)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertEqual(entry.read_text(), source)
                    node.wait_report(runtime)

    def exercise(self, language: str, backend: str) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        policy = policy_for(capture)
        source = ROOT / 'examples' / language / ('BenchmarkApp.ts' if language == 'typescript' else 'BenchmarkApp.js')
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix='otelc-source-', dir='/tmp') as temporary:
            runtime = Path(temporary).resolve()
            plain = subprocess.run(node.command(policy, runtime, language, plain=True, backend=backend, gc_fixture=True),
                input='2\nthrow\nlifecycle\ngc\nquit\n', capture_output=True, text=True, timeout=30, check=True)
            baseline = [json.loads(line) for line in plain.stdout.splitlines()]
            self.assertEqual(plain.stderr, '')
            with subprocess.Popen(node.command(policy, runtime, language, backend=backend, gc_fixture=True),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                try:
                    endpoint = runtime / 'control.json'
                    ready = control.wait_ready(endpoint, process.pid)
                    self.assertFalse(ready['collection'])
                    self.assertFalse(ready['emission'])
                    for collection, emission, measured in ((False, False, 0), (True, True, 2),
                            (True, False, 4), (False, True, 4), (True, True, 6)):
                        control.request(endpoint, 'collection ' + ('on' if collection else 'off'))
                        status = control.request(endpoint, 'emission ' + ('on' if emission else 'off'))
                        self.assertEqual(status['pid'], process.pid)
                        previous = len(capture.metrics)
                        reply = batch(process, 2)
                        self.assertEqual(reply['checksum'], checksum(2))
                        self.assertEqual(reply['calls'], 2)
                        self.assertGreater(reply['elapsed_ns'], 0)
                        control.request(endpoint, 'flush')
                        if collection and emission:
                            self.assertEqual(wait_for_count(capture.calls, measured), measured)
                        else:
                            self.assertEqual(len(capture.metrics), previous)
                    self.assertEqual(batch(process, 'throw'), baseline[1])
                    self.assertEqual(batch(process, 'lifecycle'), baseline[2])
                    self.assertEqual(batch(process, 'gc'), baseline[3])
                    deadline = time.monotonic() + 10
                    status = control.request(endpoint, 'status')
                    while status['lifetimes']['gc_completed'] < 12 and time.monotonic() < deadline:
                        time.sleep(.02)
                        status = control.request(endpoint, 'status')
                    self.assertEqual(status['lifetimes']['gc_completed'], 12, 'bounded fixture GC observation is incomplete')
                    control.request(endpoint, 'flush')
                    self.assertEqual(wait_for_count(capture.calls, 7), 7)
                    stdout, stderr = process.communicate('quit\n', timeout=15)
                    self.assertEqual((process.returncode, stdout, stderr), (0, '', ''))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            status = node.wait_report(runtime)
            self.assertFalse(endpoint.exists())
            self.assertEqual(status['phase'], 'closed')
            self.assertEqual(status['function_completions'], 11)
            self.assertTrue(all(value == 0 for value in status['runtime_losses'].values()))
            self.assertEqual(status['lifetime_lost'], 0)
            self.assertEqual(status['handoff']['lost'], 0)
            self.assertEqual(status['handoff_incomplete'], 0)
            self.assertEqual(status['lifetimes']['resource_completed'], 11)
            self.assertEqual(status['lifetimes']['failed_initialisations'], 1)
            self.assertEqual(status['lifetimes']['failed_releases'], 1)
            spans = [span for message in capture.traces for resource in message.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            functions = [span for span in spans if span.name.endswith('.workload')]
            lifetimes = [span for span in spans if span.name.endswith(' lifetime')]
            self.assertEqual(len(functions), 11)
            self.assertEqual(sum(span.status.code == 2 for span in functions), 1)
            self.assertEqual(len(lifetimes), 23)
            self.assertEqual(len(spans), 34, 'excluded application functions must not become spans')
            self.assertTrue(all(not span.parent_span_id for span in functions))
            function_ids = {span.span_id for span in functions}
            instances = {}
            for span in lifetimes:
                attributes = {item.key: item.value.string_value for item in span.attributes}
                instance = attributes['otelc.lifetime.instance']
                instances.setdefault(instance, []).append(attributes['boundary'])
                self.assertGreater(span.end_time_unix_nano, span.start_time_unix_nano)
                if attributes['type'].endswith('.WorkItem'):
                    self.assertEqual(len(span.links), 1)
                    self.assertIn(span.links[0].span_id, function_ids)
            self.assertEqual(sorted(map(len, instances.values())), [1] + [2] * 11)
            for message in capture.metrics:
                for resource in message.resource_metrics:
                    attributes = {item.key: item.value.string_value for item in resource.resource.attributes}
                    self.assertEqual(attributes['service.instance.id'], status['launch_id'])
                    self.assertEqual(attributes['otelc.language'], language)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_javascript_config_only_functions_resources_gc_and_live_states(self) -> None:
        self.exercise('javascript', 'native')

    def test_microsoft_native_compiler_config_only_functions_resources_gc_and_live_states(self) -> None:
        self.exercise('typescript', 'native')

    def test_microsoft_api_compiler_config_only_functions_resources_gc_and_live_states(self) -> None:
        self.exercise('typescript', 'api')
