"""SDK drain must not extend the application's loop or invent shutdown callbacks."""

from __future__ import annotations

import copy
import hashlib
import signal
import os
import json
import threading
import subprocess
import tempfile
import unittest
from pathlib import Path

from otelc import control, node
from otelc.config import Policy
from test_java_integration import Capture, batch

ROOT = Path(__file__).resolve().parent.parent


def shutdown_policy(capture: Capture) -> Policy:
    """Use the shared policy with exact fixture selection and full trace sampling."""
    data = copy.deepcopy(Policy.load(ROOT / 'examples/otelc.toml').data)
    data['sources'] = {'include': ['**'], 'exclude': []}
    data['lifetimes']['enabled'] = False
    data['traces']['sample_ratio'] = 1.0
    data['export'].update(endpoint=f'http://127.0.0.1:{capture.server.server_port}',
                          interval_ms=60000, timeout_ms=2000)
    return Policy.parse(data)


class NodeShutdownTests(unittest.TestCase):
    def test_existing_propagator_owner_is_preserved_when_http_startup_is_refused(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(shutdown_policy(capture).data)
        data['context']['http'] = True
        with tempfile.TemporaryDirectory(prefix='otelc-propagator-owner-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            entry = root / 'app.cjs'
            entry.write_text("console.log('APPLICATION RAN');\n")
            preload = root / 'owner.mjs'
            preload.write_text('import {createRequire} from "node:module";\n'
                'const {propagation}=createRequire(' + json.dumps(str(ROOT / 'package.json'))
                + ')("@opentelemetry/api");\n' + """const owner = {
  fields(){return ['application-owner'];}, inject(){}, extract(active){return active;}
};
propagation.setGlobalPropagator(owner);
process.on('exit', () => console.log(JSON.stringify({fields:propagation.fields()})));
""")
            runtime = root / 'instrumented'
            command = node.command(Policy.parse(data), runtime, 'javascript', root=root, entry=entry)
            result = subprocess.run([command[0], '--import', str(preload), *command[1:]],
                capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('an OpenTelemetry propagator is already installed', result.stderr)
            self.assertEqual(json.loads(result.stdout), {'fields': ['application-owner']})
            self.assertEqual(node.wait_report(runtime)['sdk_phase'], 'closed')

    def test_existing_context_owner_is_preserved_and_refusal_cannot_exit_silently(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(shutdown_policy(capture).data)
        data['metrics'].update(collection=False, emission=False)
        data['traces']['enabled'] = False
        with tempfile.TemporaryDirectory(prefix='otelc-existing-owner-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            entry = root / 'app.cjs'
            entry.write_text("console.log('APPLICATION RAN');\n")
            preload = root / 'owner.mjs'
            preload.write_text('import {createRequire} from "node:module";\n'
                'const {context,ROOT_CONTEXT}=createRequire(' + json.dumps(str(ROOT / 'package.json'))
                + ')("@opentelemetry/api");\n' + """let disabled = 0;
const active = ROOT_CONTEXT.setValue(Symbol.for('test.owner'), 7);
const owner = {enable(){return this;},disable(){disabled++;return this;},active(){return active;},
  with(_context,fn,receiver,...args){return fn.apply(receiver,args);},bind(_context,target){return target;}};
context.setGlobalContextManager(owner);
process.on('exit', () => console.log(JSON.stringify({disabled,ownerPreserved:context.active()===active})));
""")
            runtime = root / 'instrumented'
            command = node.command(Policy.parse(data), runtime, 'javascript', root=root, entry=entry)
            result = subprocess.run([command[0], '--import', str(preload), *command[1:]],
                capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0, 'startup refusal must not silently succeed before application execution')
            self.assertIn('an OpenTelemetry context manager is already installed', result.stderr)
            self.assertEqual(json.loads(result.stdout), {'disabled': 0, 'ownerPreserved': True})
            self.assertEqual(node.wait_report(runtime)['sdk_phase'], 'closed')

    def test_parent_can_exit_with_control_flush_and_actual_http_response_still_pending(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        capture.metric_response_gate.clear()
        with tempfile.TemporaryDirectory(prefix='otelc-exit-flush-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            entry = root / 'app.cjs'
            entry.write_text("""const input = require('node:readline').createInterface({input:process.stdin});
function workload() { return 42; }
input.on('line', line => {
  if (line === 'quit') input.close(); else console.log(JSON.stringify({value:workload()}));
});
""")
            runtime = root / 'instrumented'
            replies: list[bool] = []

            def flush() -> None:
                try:
                    control.request(runtime / 'control.json', 'flush')
                    replies.append(True)
                except (ValueError, OSError):
                    # The application's OS exit closes its control connection.
                    replies.append(False)

            with subprocess.Popen(node.command(shutdown_policy(capture), runtime, 'javascript', root=root, entry=entry),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                worker = threading.Thread(target=flush)
                try:
                    control.wait_ready(runtime / 'control.json', process.pid)
                    self.assertEqual(batch(process, 'run'), {'value': 42})
                    worker.start()
                    self.assertTrue(capture.metric_request_seen.wait(5))
                    stdout, stderr = process.communicate('quit\n', timeout=5)
                    self.assertEqual((process.returncode, stdout, stderr), (0, '', ''))
                    self.assertFalse(capture.metric_response_gate.is_set())
                finally:
                    capture.metric_response_gate.set()
                    if worker.ident is not None:
                        worker.join(5)
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                self.assertFalse(worker.is_alive())
            self.assertEqual(replies, [False])
            report = node.wait_report(runtime)
            self.assertEqual(report['phase'], 'closed')
            self.assertEqual(report['function_completions'], 1)
            self.assertEqual(report['span_accounting']['queue_dropped'], 0)
            self.assertEqual((runtime / 'node-sdk.log').read_text(), '')

    def compare_shutdown(self, source: str, calls: int, expected: str, exit_code: int = 0) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        capture.metric_response_gate.clear()
        with tempfile.TemporaryDirectory(prefix='otelc-exit-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            entry = root / 'app.cjs'
            entry.write_text(source)
            digest = hashlib.sha256(entry.read_bytes()).hexdigest()
            policy = shutdown_policy(capture)
            plain = subprocess.run(node.command(policy, root / 'plain', 'javascript', root=root,
                entry=entry, plain=True), capture_output=True, text=True, timeout=10)
            runtime = root / 'instrumented'
            try:
                command = node.command(policy, runtime, 'javascript', root=root, entry=entry)
                with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                    stdout, stderr = process.communicate(timeout=10)
                    parent_pid = process.pid
                    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                # The application's process exited while the actual HTTP response
                # remains blocked. The SDK process owns the outstanding request.
                self.assertFalse(capture.metric_response_gate.is_set())
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (plain.returncode, plain.stdout, plain.stderr))
                self.assertEqual((result.returncode, result.stdout), (exit_code, expected))
                self.assertTrue(capture.metric_request_seen.wait(5))
                self.assertEqual((runtime / 'node-status.json').read_bytes(), b'')
            finally:
                capture.metric_response_gate.set()
            report = node.wait_report(runtime)
            self.assertEqual(report['phase'], 'closed')
            self.assertEqual(report['function_completions'], calls)
            self.assertEqual(report['pid'], parent_pid)
            self.assertNotEqual(report['pid'], report['sdk_pid'])
            self.assertEqual(report['handoff']['lost'], 0)
            self.assertEqual(report['handoff_incomplete'], 0)
            self.assertEqual(report['traces']['exported_items'], calls)
            self.assertEqual(capture.calls(), calls)
            self.assertFalse((runtime / 'control.json').exists())
            self.assertEqual((runtime / 'node-sdk.log').read_text(), '')
            self.assertEqual(hashlib.sha256(entry.read_bytes()).hexdigest(), digest)

    def test_slow_drain_does_not_run_unref_timer_or_repeat_before_exit(self) -> None:
        self.compare_shutdown("""function workload() { return 42; }
console.log(workload());
setTimeout(() => console.log('late-unref'), 50).unref();
process.on('beforeExit', () => console.log('before-exit'));
process.on('exit', () => console.log('exit'));
""", 1, '42\nbefore-exit\nexit\n')

    def test_before_exit_may_intentionally_extend_application_work(self) -> None:
        self.compare_shutdown("""function workload() { return 42; }
console.log(workload());
let first = true;
process.on('beforeExit', () => {
  console.log('before-exit');
  if (first) { first = false; setImmediate(() => console.log(workload())); }
});
""", 2, '42\nbefore-exit\n42\nbefore-exit\n')

    def test_explicit_exit_records_work_in_application_exit_listener(self) -> None:
        self.compare_shutdown("""function workload() { return 42; }
console.log(workload());
process.on('exit', () => console.log(workload()));
process.exit(7);
""", 2, '42\n42\n', 7)

    def test_synthetic_exit_event_does_not_close_instrumentation(self) -> None:
        self.compare_shutdown("""function workload() { return 42; }
console.log(workload());
process.on('exit', () => console.log('exit-listener'));
process.emit('exit', 99);
console.log(workload());
""", 2, '42\nexit-listener\n42\nexit-listener\n')

    def test_trace_ids_and_export_do_not_use_application_random(self) -> None:
        self.compare_shutdown("""Math.random = () => { throw new Error('application random must not run'); };
function workload() { return 42; }
console.log(workload());
""", 1, '42\n')

    def test_signal_exit_does_not_claim_a_clean_producer_checkpoint(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='otelc-signal-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            entry = root / 'app.cjs'
            entry.write_text('setInterval(() => {}, 1000);\n')
            runtime = root / 'instrumented'
            with subprocess.Popen(node.command(shutdown_policy(capture), runtime, 'javascript',
                    root=root, entry=entry), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                control.wait_ready(runtime / 'control.json', process.pid)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual((process.returncode, stdout, stderr), (-signal.SIGTERM, '', ''))
            report = node.wait_report(runtime)
            self.assertEqual(report['phase'], 'failed')
            self.assertFalse(report['producer_checkpoint_available'])
            self.assertFalse((runtime / 'control.json').exists())

    def test_sdk_process_failure_does_not_replace_application_values_or_hang_controls(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='otelc-sdk-death-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            entry = root / 'app.cjs'
            entry.write_text("""const input = require('node:readline').createInterface({input: process.stdin});
function workload() { return 42; }
input.on('line', line => { if (line === 'quit') input.close(); else console.log(workload()); });
""")
            runtime = root / 'instrumented'
            with subprocess.Popen(node.command(shutdown_policy(capture), runtime, 'javascript', root=root, entry=entry),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                try:
                    ready = control.wait_ready(runtime / 'control.json', process.pid)
                    os.kill(ready['sdk_pid'], signal.SIGKILL)
                    with self.assertRaisesRegex(ValueError, 'runtime rejected control request'):
                        control.request(runtime / 'control.json', 'status')
                    stdout, stderr = process.communicate('run\nquit\n', timeout=10)
                    self.assertEqual((process.returncode, stdout, stderr), (0, '42\n', ''))
                    with self.assertRaisesRegex(TimeoutError, 'not completed'):
                        node.wait_report(runtime, timeout=.1)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

    def test_capacity_loss_and_uncompleted_lifetimes_are_not_reported_as_a_clean_run(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(shutdown_policy(capture).data)
        data['lifetimes'].update(enabled=True, max_live=1)
        with tempfile.TemporaryDirectory(prefix='otelc-capacity-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            entry = root / 'app.cjs'
            entry.write_text("""class WorkItem { close() {} }
function workload() { return new WorkItem(); }
globalThis.held = [workload(), workload()];
console.log(globalThis.held.length);
""")
            runtime = root / 'instrumented'
            result = subprocess.run(node.command(Policy.parse(data), runtime, 'javascript', root=root, entry=entry),
                text=True, capture_output=True, timeout=10)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '2\n', ''))
            report = node.wait_report(runtime)
            self.assertEqual(report['phase'], 'failed')
            self.assertEqual(report['sdk_phase'], 'closed')
            self.assertEqual(report['lifetime_lost'], 1)
            self.assertEqual(report['lifetimes']['censored'], 1)
            self.assertEqual(report['lifetimes']['current_live'], 1)
            self.assertEqual(report['lifetimes']['tracked_instances'], 0)
            self.assertEqual(report['lifetimes']['gc_completed'], 0)

    def test_official_span_queue_loss_is_visible_even_while_metrics_are_off(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        capture.trace_response_gate.clear()
        data = copy.deepcopy(shutdown_policy(capture).data)
        data['metrics'].update(collection=False, emission=False)
        data['export'].update(timeout_ms=10000, shutdown_timeout_ms=15000)
        with tempfile.TemporaryDirectory(prefix='otelc-span-pressure-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            entry = root / 'app.cjs'
            entry.write_text('function workload(i) { return i; }\n'
                             'let sum = 0; for (let i = 0; i < 6000; i++) sum += workload(i);\n'
                             'console.log(sum);\n')
            runtime = root / 'instrumented'
            try:
                result = subprocess.run(node.command(Policy.parse(data), runtime, 'javascript', root=root, entry=entry),
                    text=True, capture_output=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '17997000\n', ''))
                self.assertTrue(capture.trace_request_seen.wait(5))
            finally:
                capture.trace_response_gate.set()
            report = node.wait_report(runtime)
            self.assertEqual(report['phase'], 'failed')
            self.assertEqual(report['sdk_phase'], 'closed')
            self.assertEqual(report['handoff']['lost'], 0)
            self.assertEqual(report['handoff_incomplete'], 0)
            self.assertEqual(report['span_observation']['sampled_ended'], 6000)
            counts = report['span_accounting']
            self.assertEqual(counts['received'], 6000)
            self.assertGreater(counts['queue_dropped'], 0)
            self.assertEqual(counts['queue_evidence'], 'derived_after_shutdown')
            self.assertEqual(counts['queue_pending'], 0)
            self.assertEqual(counts['sdk_exported'] + counts['queue_dropped'], 6000)
            decoded = [span for message in capture.traces for resource in message.resource_spans
                       for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(decoded), counts['sdk_exported'])
            self.assertEqual(capture.metrics, [])
