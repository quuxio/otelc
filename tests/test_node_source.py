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
