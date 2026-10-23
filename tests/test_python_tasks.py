"""Official thread patches preserve context and original application behaviour."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Thread, Timer
from types import SimpleNamespace
from unittest import mock

from opentelemetry import baggage, context, trace

from otelc.python_tasks import PythonTaskContext
from otelc.python_startup import PythonApplication, close_task_context
from test_java_integration import Capture

ROOT = Path(os.environ.get('OTELC_TASK_TEST_PROVIDER', Path(__file__).resolve().parents[1]))
SOURCE = Path(__file__).resolve().parents[1] / 'examples/python/TaskApp.py'


class PythonTaskTests(unittest.TestCase):
    def test_shared_task_policy_runs_the_unchanged_documented_example(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        original = SOURCE.read_bytes()
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            policy = root / 'tasks.toml'
            text = (SOURCE.parents[1] / 'tasks.toml').read_text()
            text = text.replace('http://127.0.0.1:4320', f'http://127.0.0.1:{capture.server.server_port}')
            policy.write_text(text)
            result = subprocess.run([sys.executable, '-I', '-S', '-B', str(ROOT / 'otelc/cli_bootstrap.py'), '--config', str(policy),
                'python', '--root', str(SOURCE.parents[2]), '--entry', str(SOURCE),
                '--runtime', str(root / 'runtime')], cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual((result.returncode, result.stderr), (0, ''))
            self.assertEqual(json.loads(result.stdout), {'results': [70, 98], 'failure': 'negative seed'})
            self.assertEqual(SOURCE.read_bytes(), original)
            report = json.loads((root / 'runtime/sdk/python-sdk-status.json').read_text())
            self.assertEqual((report['phase'], report['producer']['function_completions']), ('closed', 16))
            # Broad qualified-name globs also select the two target lambdas
            # nested inside workload; they are ordinary selected functions.
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), 16)

    def test_native_application_policy_states_output_source_and_actual_parentage(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            application = root / 'application'
            application.mkdir()
            source = application / 'entry.py'
            original = SOURCE.read_bytes()
            source.write_bytes(original)
            baseline = subprocess.run([sys.executable, str(source)], cwd=application,
                                      capture_output=True, text=True, timeout=10)
            self.assertEqual(json.loads(baseline.stdout),
                             {'results': [70, 98], 'failure': 'negative seed'})
            for tasks in (False, True):
                with self.subTest(tasks=tasks):
                    start = len(capture.traces)
                    policy = root / ('on.toml' if tasks else 'off.toml')
                    policy.write_text(f'''schema_version=3
[sources]
include=["**/*.py"]
[functions]
include=["entry.workload","entry.child","entry.async_child"]
[context]
tasks={str(tasks).lower()}
[metrics]
collection=false
emission=false
[traces]
enabled=true
sample_ratio=1.0
[export]
endpoint="http://127.0.0.1:{capture.server.server_port}"
interval_ms=100000
''')
                    runtime = root / ('on' if tasks else 'off')
                    result = subprocess.run([sys.executable, '-I', '-S', '-B', str(ROOT / 'otelc/cli_bootstrap.py'), '--config', str(policy),
                        'python', '--root', str(application), '--entry', str(source),
                        '--runtime', str(runtime)], cwd=ROOT, capture_output=True, text=True, timeout=20)
                    self.assertEqual((result.returncode, result.stdout, result.stderr),
                                     (baseline.returncode, baseline.stdout, baseline.stderr))
                    self.assertEqual(source.read_bytes(), original)
                    report = json.loads((runtime / 'sdk/python-sdk-status.json').read_text())
                    self.assertEqual(report['phase'], 'closed')
                    self.assertEqual(report['producer']['function_completions'], 12)
                    spans = [span for request in capture.traces[start:] for resource in request.resource_spans
                             for scope in resource.scope_spans for span in scope.spans]
                    self.assertEqual(len(spans), 12)
                    parents = {span.span_id: span for span in spans if span.name == 'entry.workload'}
                    self.assertEqual(len(parents), 3)
                    self.assertTrue(all(not span.parent_span_id for span in parents.values()))
                    for span in spans:
                        if span.name == 'entry.child' and not tasks:
                            self.assertFalse(span.parent_span_id)
                        elif span.name in ('entry.child', 'entry.async_child'):
                            self.assertIn(span.parent_span_id, parents)
                            self.assertEqual(span.trace_id, parents[span.parent_span_id].trace_id)
                    if tasks:
                        for parent in parents.values():
                            children = [span for span in spans if span.parent_span_id == parent.span_id]
                            self.assertEqual(len(children), 1 if parent.status.code == 2 else 4)
                    self.assertEqual(sum(span.status.code == 2 for span in spans), 2)

    def test_actual_thread_timer_and_reused_pool_preserve_baggage_and_clear_after_failure(self) -> None:
        owner = PythonTaskContext(True)
        self.addCleanup(owner.close)

        def observe():
            return (trace.get_current_span().get_span_context().trace_id,
                    baggage.get_baggage('request'))

        def attach(identity):
            span = trace.NonRecordingSpan(trace.SpanContext(identity, identity, False, trace.TraceFlags(1)))
            return context.attach(baggage.set_baggage('request', str(identity), trace.set_span_in_context(span)))

        with ThreadPoolExecutor(max_workers=1) as pool:
            for identity in (17, 23):
                token = attach(identity)
                try:
                    self.assertEqual(pool.submit(observe).result(timeout=3), (identity, str(identity)))
                    received = []
                    for cls, args in ((Thread, ()), (Timer, (.001,))):
                        if cls is Thread:
                            worker = cls(target=lambda: received.append(observe()))
                        else:
                            worker = cls(*args, function=lambda: received.append(observe()))
                        worker.start()
                        worker.join(timeout=3)
                        self.assertFalse(worker.is_alive())
                    self.assertEqual(received, [(identity, str(identity))] * 2)
                finally:
                    context.detach(token)
            error = ValueError('original')

            def fail():
                raise error

            token = attach(31)
            try:
                with self.assertRaises(ValueError) as raised:
                    pool.submit(fail).result(timeout=3)
                self.assertIs(raised.exception, error)
            finally:
                context.detach(token)
            self.assertEqual(pool.submit(observe).result(timeout=3), (0, None))

    def test_disabled_existing_failed_and_owned_instrumentation_lifecycles(self) -> None:
        with mock.patch('otelc.python_tasks.ThreadingInstrumentor') as factory:
            disabled = PythonTaskContext(False)
            disabled.close()
            factory.assert_not_called()
            instrumentor = SimpleNamespace(is_instrumented_by_opentelemetry=True,
                                           instrument=mock.Mock(), uninstrument=mock.Mock())
            factory.return_value = instrumentor
            existing = PythonTaskContext(True)
            existing.close()
            instrumentor.instrument.assert_not_called()
            instrumentor.uninstrument.assert_not_called()
            instrumentor.is_instrumented_by_opentelemetry = False
            with self.assertRaisesRegex(RuntimeError, 'did not start'):
                PythonTaskContext(True)

            def install():
                instrumentor.is_instrumented_by_opentelemetry = True

            instrumentor.instrument.side_effect = install
            owner = PythonTaskContext(True)
            owner.close()
            owner.close()
            instrumentor.uninstrument.assert_called_once()

            def partial():
                install()
                raise RuntimeError('partial installation')

            instrumentor.is_instrumented_by_opentelemetry = False
            instrumentor.instrument.side_effect = partial
            with self.assertRaisesRegex(RuntimeError, 'partial installation'):
                PythonTaskContext(True)
            self.assertEqual(instrumentor.uninstrument.call_count, 2)

    def test_task_teardown_failure_does_not_prevent_runtime_checkpoint(self) -> None:
        runtime, monitor, server, tasks = (mock.Mock() for _ in range(4))
        tasks.close.side_effect = RuntimeError('controlled library failure')
        application = PythonApplication(runtime, monitor, server, tasks)
        application.close()
        application.close()
        runtime.reject.assert_called_once_with('task_context_shutdown')
        runtime.close.assert_called_once()
        monitor.close.assert_called_once()
        server.close.assert_called_once()
        tasks.close.assert_called_once()
        close_task_context(None, runtime)
