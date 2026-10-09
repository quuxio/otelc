"""Worker handoffs keep causal parents without retaining tasks or payloads."""
import asyncio
import concurrent.futures
import gc
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import weakref
import threading
import unittest
from unittest.mock import patch

from test_python_spans import PythonSpanHarness, trace_plan, receiver, ROOT, ExportTraceServiceRequest


def worker_policy():
    policy = trace_plan()
    policy["propagation"] = {"tasks": True, "http": False}
    return policy


class WorkerContextTests(PythonSpanHarness, unittest.TestCase):
    def test_pool_child_outlives_parent_without_changing_result(self):
        runtime, _, capture, app, monitor = self.monitored("""
def child(gate, values):
    gate.wait(5)
    values.append(42)
def root(pool, gate, values):
    return pool.submit(child, gate, values)
""", worker_policy())
        gate, values = threading.Event(), []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = app["root"](pool, gate, values)
            try:
                self.assertEqual(runtime.traces.report()["pending_contexts"], 1)
            finally:
                gate.set()
            future.result(5)
        self.assertEqual(values, [42])
        report = self.finish(runtime, monitor)
        parent, child = sorted(capture.spans(), key=lambda node: bool(node.parent_span_id))
        self.assertEqual(child.parent_span_id, parent.span_id)
        self.assertEqual(child.trace_id, parent.trace_id)
        self.assertGreater(child.end_time_unix_nano, parent.end_time_unix_nano)
        self.assertEqual(report["traces"]["losses"], {})

    def test_reused_pool_workers_keep_independent_roots_and_original_contextvars(self):
        runtime, _, capture, app, monitor = self.monitored("""
import contextvars
value = contextvars.ContextVar('application-value', default='worker default')
def child(number):
    return number + 1, value.get()
def root(pool, number):
    token = value.set('creator value')
    try:
        return pool.submit(child, number)
    finally:
        value.reset(token)
""", worker_policy())
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            for value in (10, 20):
                self.assertEqual(app["root"](pool, value).result(5), (value + 1, "worker default"))
            self.assertEqual(pool.submit(app["child"], 30).result(5), (31, "worker default"))
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        roots = [node for node in spans if node.name.endswith(".root")]
        children = [node for node in spans if node.name.endswith(".child")]
        self.assertEqual(len(roots), 2)
        self.assertNotEqual(roots[0].trace_id, roots[1].trace_id)
        self.assertEqual(sum(bool(node.parent_span_id) for node in children), 2)
        self.assertEqual(report["traces"]["completed_trees"], 3)
        self.assertEqual(report["traces"]["pending_contexts"], 0)
        self.assertEqual(report["traces"]["losses"], {})

    def test_cancelled_queued_work_releases_its_parent_reservation(self):
        runtime, _, capture, app, monitor = self.monitored("""
def child():
    raise AssertionError('cancelled callback must not execute')
def root(pool):
    return pool.submit(child)
""", worker_policy())
        gate, started = threading.Event(), threading.Event()
        def occupied():
            started.set()
            gate.wait(5)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            occupying = pool.submit(occupied)
            self.assertTrue(started.wait(5))
            future = app["root"](pool)
            self.assertTrue(future.cancel())
            self.assertEqual(runtime.traces.report()["pending_contexts"], 0)
            gate.set()
            occupying.result(5)
        report = self.finish(runtime, monitor)
        self.assertEqual([node.name for node in capture.spans()], ["trace_fixture.root"])
        self.assertEqual(report["traces"]["losses"], {})

    def test_submission_failures_preserve_exception_identity_and_release_context(self):
        runtime, _, capture, app, monitor = self.monitored("""
def child():
    return 1
def root(pool):
    return pool.submit(child)
""", worker_policy())
        error = RuntimeError("original submit failure")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(pool, "_adjust_thread_count", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    app["root"](pool)
                self.assertIs(caught.exception, error)
            # The failed adjustment may still have queued work; standard shutdown
            # cancels it rather than granting it an observer-owned lifetime.
            pool.shutdown(cancel_futures=True)
        report = self.finish(runtime, monitor)
        self.assertEqual(report["traces"]["pending_contexts"], 0)

    def test_worker_exceptions_keep_original_identity_and_release_context(self):
        runtime, _, capture, app, monitor = self.monitored("""
def child(error):
    raise error
def root(pool, error):
    return pool.submit(child, error)
""", worker_policy())
        error = ValueError("original worker failure")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = app["root"](pool, error)
            with self.assertRaises(ValueError) as caught:
                future.result(5)
            self.assertIs(caught.exception, error)
        report = self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 2)
        self.assertEqual(report["traces"]["pending_contexts"], 0)
        self.assertEqual(report["traces"]["losses"], {})

    def test_to_thread_and_run_in_executor_preserve_results_and_existing_application_context(self):
        runtime, _, capture, app, monitor = self.monitored("""
import contextvars
value = contextvars.ContextVar('application', default='default')
def child():
    return value.get()
async def root():
    token = value.set('original')
    try:
        return await asyncio.to_thread(child), await asyncio.get_running_loop().run_in_executor(None, child)
    finally:
        value.reset(token)
""", worker_policy())
        self.assertEqual(asyncio.run(app["root"]()), ("original", "default"))
        report = self.finish(runtime, monitor)
        nodes = capture.spans()
        parent = next(node for node in nodes if node.name.endswith('.root'))
        self.assertEqual(len(nodes), 3)
        self.assertTrue(all(node.parent_span_id == parent.span_id for node in nodes if node is not parent))
        self.assertEqual(report["traces"]["losses"], {})

    def test_completed_workers_do_not_retain_payloads_callables_or_futures(self):
        runtime, _, capture, app, monitor = self.monitored("""
def root(pool, callable, payload):
    return pool.submit(callable, payload)
""", worker_policy())
        class Payload:
            pass
        class Callable:
            def __call__(self, payload):
                return 42
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            payload, callable = Payload(), Callable()
            future = app["root"](pool, callable, payload)
            self.assertEqual(future.result(5), 42)
            refs = [weakref.ref(value) for value in (payload, callable, future)]
            del payload, callable, future
            # A sentinel proves the worker has released its previous queue item.
            pool.submit(lambda: None).result(5)
            gc.collect()
            self.assertTrue(all(reference() is None for reference in refs))
        self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 1)

    def test_sampling_zero_never_resamples_worker_children(self):
        policy = worker_policy()
        policy["traces"]["root_sample_ratio"] = 0.0
        runtime, _, capture, app, monitor = self.monitored("""
def child():
    return 42
def root(pool):
    return pool.submit(child)
""", policy)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(app["root"](pool).result(5), 42)
        report = self.finish(runtime, monitor)
        self.assertEqual(capture.spans(), [])
        self.assertEqual(report["traces"]["sampled_out_roots"], 1)
        self.assertEqual(report["traces"]["losses"], {})

    def test_context_capacity_discards_whole_tree_and_releases_slots(self):
        policy = worker_policy()
        policy["runtime"]["max_active_calls"] = 1
        runtime, _, capture, app, monitor = self.monitored("""
def child(gate):
    gate.wait(5)
    return 42
def root(pool, gate):
    return pool.submit(child, gate), pool.submit(child, gate)
""", policy)
        gate, started = threading.Event(), threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            blocker = pool.submit(lambda: (started.set(), gate.wait(5)))
            self.assertTrue(started.wait(5))
            futures = app["root"](pool, gate)
            gate.set()
            blocker.result(5)
            self.assertEqual([future.result(5) for future in futures], [42, 42])
        report = self.finish(runtime, monitor)
        self.assertEqual(capture.spans(), [])
        self.assertEqual(report["traces"]["losses"]["context_capacity"], 1)
        self.assertEqual(report["traces"]["pending_contexts"], 0)

    def test_failed_callback_installation_keeps_future_but_discards_partial_tree(self):
        runtime, _, capture, app, monitor = self.monitored("""
def child():
    return 42
def root(pool):
    return pool.submit(child)
""", worker_policy())
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(concurrent.futures.Future, 'add_done_callback', side_effect=RuntimeError('original callback failure')):
                future = app["root"](pool)
                self.assertEqual(future.result(5), 42)
        report = self.finish(runtime, monitor)
        self.assertEqual(capture.spans(), [])
        self.assertEqual(report["traces"]["losses"]["context_hook"], 1)
        self.assertEqual(report["traces"]["pending_contexts"], 0)

    def test_duplicate_worker_hook_rolls_back_asyncio_hook_and_preserves_new_hooks_on_close(self):
        from quux_otelc_python.task_context import TaskContext
        runtime, _, _ = self.runtime(worker_policy())
        original = asyncio.BaseEventLoop.create_task
        def installed(*args, **kwargs):
            pass
        installed._otelc_worker_context = True
        context = TaskContext(runtime)
        with patch.object(concurrent.futures.ThreadPoolExecutor, 'submit', installed):
            with self.assertRaisesRegex(ValueError, 'already installed'):
                context.install()
            self.assertIs(asyncio.BaseEventLoop.create_task, original)
            context.close()
            self.assertIs(concurrent.futures.ThreadPoolExecutor.submit, installed)

    def test_recursive_submission_links_to_current_worker_and_restores_context(self):
        runtime, _, capture, app, monitor = self.monitored("""
def leaf(value):
    return value + 1
def child(pool):
    return pool.submit(leaf, 41).result(5)
def root(pool):
    return pool.submit(child, pool)
""", worker_policy())
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(app["root"](pool).result(5), 42)
        report = self.finish(runtime, monitor)
        nodes = {span.name.rsplit('.', 1)[-1]: span for span in capture.spans()}
        self.assertEqual(nodes['leaf'].parent_span_id, nodes['child'].span_id)
        self.assertEqual(nodes['child'].parent_span_id, nodes['root'].span_id)
        self.assertEqual(report["traces"]["completed_trees"], 1)
        self.assertEqual(report["traces"]["losses"], {})

    def test_shutdown_with_pending_worker_discards_tree_without_waiting_for_application(self):
        original = concurrent.futures.ThreadPoolExecutor.submit
        runtime, _, capture, app, monitor = self.monitored("""
def child(gate):
    gate.wait(5)
    return 42
def root(pool, gate):
    return pool.submit(child, gate)
""", worker_policy())
        gate = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = app["root"](pool, gate)
            try:
                report = self.finish(runtime, monitor)
                self.assertEqual(capture.spans(), [])
                self.assertEqual(report["traces"]["losses"]["incomplete"], 1)
                self.assertIs(concurrent.futures.ThreadPoolExecutor.submit, original)
            finally:
                gate.set()
            self.assertEqual(future.result(5), 42)

    def test_actual_cli_preserves_ordinary_worker_source_results_and_causal_otlp(self):
        cli = ROOT / 'target/debug/quux-otelc'
        if not cli.is_file():
            self.skipTest('CLI is built by the native product gate')
        source = ROOT / 'examples/apps/python_workers_app.py'
        original = source.read_bytes()
        env = dict(os.environ, OTELC_PYTHON=sys.executable, OTELC_ADAPTER_ROOT=str(ROOT / 'adapters'))
        plain = subprocess.run([sys.executable, str(source)], cwd=ROOT, env=env,
                               capture_output=True, timeout=30)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        with tempfile.TemporaryDirectory() as temporary, receiver() as (endpoint, requests):
            report_path = Path(temporary) / 'runtime.json'
            env.update(OTEL_EXPORTER_OTLP_ENDPOINT=endpoint, OTELC_REPORT_PATH=str(report_path))
            result = subprocess.run([str(cli), '--config',
                                     'examples/python-worker-context.toml', 'python', str(source)],
                                    cwd=ROOT, env=env, capture_output=True, timeout=30)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (plain.returncode, plain.stdout, plain.stderr))
            status = json.loads(report_path.read_text())
            self.assertEqual(status['function_calls'], 10)
            self.assertEqual(status['export_loss'], 0)
            self.assertEqual(status['traces']['pending_contexts'], 0)
            self.assertEqual(status['traces']['losses'], {})
            nodes = [span for path, _, body in requests if path == '/v1/traces'
                     for resource in ExportTraceServiceRequest.FromString(body).resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            roots = [node for node in nodes if not node.parent_span_id]
            self.assertEqual((len(nodes), len(roots)), (10, 5))
            self.assertEqual(len({node.trace_id for node in roots}), 5)
            for root in roots:
                children = [node for node in nodes if node.parent_span_id == root.span_id]
                self.assertEqual(len(children), 1)
                self.assertEqual(children[0].trace_id, root.trace_id)
            self.assertEqual(sum(node.status.code == 2 for node in nodes), 1)
        self.assertEqual(source.read_bytes(), original)

    def test_disabled_propagation_retains_independent_worker_roots(self):
        runtime, _, capture, app, monitor = self.monitored("""
def child():
    return 42
def root(pool):
    return pool.submit(child)
""", trace_plan())
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(app["root"](pool).result(5), 42)
        self.finish(runtime, monitor)
        self.assertEqual(sum(not node.parent_span_id for node in capture.spans()), 2)
