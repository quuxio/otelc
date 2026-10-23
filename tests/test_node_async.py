"""Actual unedited async applications preserve identity, context and collection."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from otelc import node
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parent.parent


def async_policy(capture: Capture, *, tasks: bool = False, metrics: bool = True) -> Policy:
    """Select ordinary source functions with the same schema as the Java application."""
    data = copy.deepcopy(Policy.load(ROOT / 'examples/otelc.toml').data)
    data['sources'] = {'include': ['**'], 'exclude': []}
    data['functions']['exclude'].append('*<anonymous>*')
    data['context']['tasks'] = tasks
    data['metrics'].update(collection=metrics, emission=metrics)
    data['traces']['sample_ratio'] = 1.
    data['export'].update(endpoint=f'http://127.0.0.1:{capture.server.server_port}', interval_ms=60000)
    return Policy.parse(data)


class NodeAsyncTests(unittest.TestCase):
    def test_observation_preserves_unhandled_rejection_and_late_handler_identity(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        result, report = self.execute("""const events = [];
const original = {identity: 7};
let result;
async function workload(value) { return value; }
process.on('unhandledRejection', (error, promise) => {
  events.push(['unhandled', error === original, promise === result]);
  setImmediate(() => promise.catch(() => {}));
});
process.on('rejectionHandled', promise => {
  events.push(['handled', promise === result]);
  console.log(JSON.stringify({events}));
});
result = workload(Promise.reject(original));
""", async_policy(capture))
        self.assertEqual(result, {'events': [['unhandled', True, True], ['handled', True]]})
        self.assertEqual(report['function_completions'], 1)

    def test_metrics_only_async_completion_does_not_require_tracing(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(async_policy(capture).data)
        data['traces']['enabled'] = False
        data['lifetimes']['enabled'] = False
        result, report = self.execute("""const { setImmediate: tick } = require('node:timers/promises');
async function workload(value) { await tick(); return value; }
(async () => { console.log(JSON.stringify({value: await workload(Promise.resolve(7))})); })();
""", Policy.parse(data))
        self.assertEqual(result, {'value': 7})
        self.assertEqual(report['function_completions'], 1)
        self.assertEqual(capture.calls(), 1)
        self.assertEqual(capture.traces, [])
        self.assertEqual(report['span_accounting']['queue_dropped'], 0)

    def execute(self, source: str, policy: Policy, language: str = 'javascript', backend: str = 'native'):
        """Run plain and instrumented original URLs, with no SDK or observer calls in the app."""
        with tempfile.TemporaryDirectory(prefix='otelc-async-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            entry = root / ('app.cjs' if language == 'javascript' else 'app.cts')
            entry.write_text(source)
            original = entry.read_bytes()
            plain = subprocess.run(node.command(policy, root / 'plain', language, root=root, entry=entry,
                plain=True, backend=backend, gc_fixture=True), text=True, capture_output=True, timeout=15)
            runtime = root / 'instrumented'
            instrumented = subprocess.run(node.command(policy, runtime, language, root=root, entry=entry,
                backend=backend, gc_fixture=True), text=True, capture_output=True, timeout=15)
            self.assertEqual((plain.returncode, plain.stderr), (0, ''))
            self.assertEqual((instrumented.returncode, instrumented.stdout, instrumented.stderr),
                             (plain.returncode, plain.stdout, plain.stderr))
            report = node.wait_report(runtime)
            self.assertEqual(report['phase'], 'closed')
            self.assertTrue(all(value == 0 for value in report['runtime_losses'].values()), report['runtime_losses'])
            self.assertEqual(report['native_callback_failures'], 0)
            self.assertEqual(report['handoff']['lost'], 0)
            self.assertEqual(report['handoff_incomplete'], 0)
            self.assertEqual(entry.read_bytes(), original)
            return json.loads(instrumented.stdout), report

    def test_async_promise_adoption_errors_als_and_task_context_across_compilers(self) -> None:
        source = """const { AsyncLocalStorage } = require('node:async_hooks');
const { setImmediate: tick } = require('node:timers/promises');
const storage = new AsyncLocalStorage();
let touched = 0;
const thrown = { get message() { touched++; return 'forbidden'; }, get stack() { touched++; return 'forbidden'; } };
async function workloadAdopt(value) { return value; }
async function workloadChild() { await tick(); return storage.getStore(); }
async function workloadParent() { await tick(); return await workloadChild(); }
function workloadTask() { return 9; }
function workloadSchedule(done) { setTimeout(() => done(workloadTask()), 5); }
(async () => {
  const original = Promise.resolve(7);
  const adopted = workloadAdopt(original);
  const value = await adopted;
  let errorIdentity = false;
  try { await workloadAdopt(Promise.reject(thrown)); } catch (error) { errorIdentity = error === thrown; }
  const stored = await storage.run('request-context', () => workloadParent());
  const task = await new Promise(resolve => workloadSchedule(resolve));
  console.log(JSON.stringify({ value, distinct: adopted !== original, errorIdentity, touched, stored, task }));
})();
"""
        for language, backend in (('javascript', 'native'), ('typescript', 'native'), ('typescript', 'api')):
            for tasks in (False, True):
                with self.subTest(language=language, compiler=backend, tasks=tasks):
                    capture = Capture()
                    try:
                        result, report = self.execute(source, async_policy(capture, tasks=tasks), language, backend)
                        self.assertEqual(result, {'value': 7, 'distinct': True, 'errorIdentity': True,
                                                  'touched': 0, 'stored': 'request-context', 'task': 9})
                        self.assertEqual(report['function_completions'], 6)
                        spans = [span for message in capture.traces for resource in message.resource_spans
                                 for scope in resource.scope_spans for span in scope.spans]
                        self.assertEqual(len(spans), 6)
                        parent = next(span for span in spans if span.name.endswith('.workloadParent'))
                        child = next(span for span in spans if span.name.endswith('.workloadChild'))
                        self.assertEqual(child.parent_span_id, parent.span_id)
                        self.assertGreaterEqual(child.start_time_unix_nano, parent.start_time_unix_nano)
                        self.assertLessEqual(child.end_time_unix_nano, parent.end_time_unix_nano)
                        task = next(span for span in spans if span.name.endswith('.workloadTask'))
                        scheduler = next(span for span in spans if span.name.endswith('.workloadSchedule'))
                        self.assertEqual(task.parent_span_id, scheduler.span_id if tasks else b'')
                        self.assertEqual(sum(span.status.code == 2 for span in spans), 1)
                    finally:
                        capture.close()

    def test_returned_object_can_collect_before_flush_in_active_and_metric_off_states(self) -> None:
        source = """const { setImmediate: tick } = require('node:timers/promises');
let finalized = 0;
const registry = new FinalizationRegistry(() => finalized++);
class WorkItem { close() {} }
async function workloadFactory() { const item = new WorkItem(); registry.register(item, 1); return item; }
(async () => {
  let result = await workloadFactory(); result.close(); result = null;
  for (let i = 0; i < 100 && finalized === 0; i++) { await tick(); global.gc(); }
  await tick();
  console.log(JSON.stringify({ finalized }));
})();
"""
        for metrics in (False, True):
            with self.subTest(metrics=metrics):
                capture = Capture()
                try:
                    result, report = self.execute(source, async_policy(capture, metrics=metrics))
                    self.assertEqual(result['finalized'], 1, 'bounded explicit fixture GC observation is incomplete')
                    self.assertEqual(report['function_completions'], 1)
                    self.assertEqual(report['lifetimes']['gc_completed'], 1)
                    self.assertEqual(report['lifetimes']['resource_completed'], 1)
                    self.assertEqual(report['lifetimes']['current_live'], 0)
                    self.assertEqual(report['lifetimes']['lost'], 0)
                    self.assertEqual(bool(capture.metrics), metrics)
                finally:
                    capture.close()
