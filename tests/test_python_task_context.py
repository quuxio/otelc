"""Task scheduling retains only bounded trace identities, never application frames."""
import asyncio
import contextvars
import gc
import json
import os
import subprocess
import sys
import tempfile
import unittest
import weakref
from pathlib import Path

from test_python_spans import PythonSpanHarness, trace_plan, receiver, ROOT, ExportTraceServiceRequest


def task_policy():
    policy = trace_plan()
    policy["propagation"] = {"tasks": True, "http": False}
    return policy


class PythonTaskContextTests(PythonSpanHarness, unittest.TestCase):
    def test_detached_child_keeps_closed_parent_without_extending_its_duration(self):
        runtime, _, capture, app, monitor = self.monitored("""
async def child():
    await asyncio.sleep(0)
    return 42
async def root():
    return asyncio.create_task(child())
""", task_policy())
        async def exercise():
            task = await app["root"]()
            self.assertEqual(await task, 42)
            await asyncio.sleep(0)
        asyncio.run(exercise())
        report = self.finish(runtime, monitor)
        root, child = sorted(capture.spans(), key=lambda span: bool(span.parent_span_id))
        self.assertEqual(child.trace_id, root.trace_id)
        self.assertEqual(child.parent_span_id, root.span_id)
        self.assertGreater(child.end_time_unix_nano, root.end_time_unix_nano)
        self.assertEqual(report["traces"]["completed_trees"], 1)
        self.assertEqual(report["traces"]["losses"], {})

    def test_parallel_roots_gather_and_taskgroup_are_isolated(self):
        runtime, _, capture, app, monitor = self.monitored("""
async def child(value):
    await asyncio.sleep(0)
    return value
async def root(value):
    async with asyncio.TaskGroup() as group:
        one = group.create_task(child(value))
        two = asyncio.create_task(child(value + 1))
        result = await asyncio.gather(one, two)
    return result
""", task_policy())
        async def exercise():
            self.assertEqual(await asyncio.gather(app["root"](10), app["root"](20)), [[10, 11], [20, 21]])
        asyncio.run(exercise())
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        roots = [span for span in spans if span.name.endswith(".root")]
        self.assertEqual(len(spans), 6)
        self.assertEqual(len(roots), 2)
        self.assertNotEqual(roots[0].trace_id, roots[1].trace_id)
        for root in roots:
            children = [span for span in spans if span.parent_span_id == root.span_id]
            self.assertEqual(len(children), 2)
            self.assertTrue(all(span.trace_id == root.trace_id for span in children))
        self.assertEqual(report["traces"]["completed_trees"], 2)
        self.assertEqual(report["traces"]["losses"], {})

    def test_explicit_empty_context_is_respected_and_copied_context_inherits(self):
        runtime, _, capture, app, monitor = self.monitored("""
import contextvars
async def child():
    await asyncio.sleep(0)
    return 7
async def root():
    return await asyncio.gather(
        asyncio.create_task(child(), context=contextvars.Context()),
        asyncio.create_task(child(), context=contextvars.copy_context()))
""", task_policy())
        self.assertEqual(asyncio.run(app["root"]()), [7, 7])
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        root = next(span for span in spans if span.name.endswith(".root"))
        children = [span for span in spans if span.name.endswith(".child")]
        self.assertEqual(sum(span.parent_span_id == root.span_id for span in children), 1)
        self.assertEqual(sum(not span.parent_span_id for span in children), 1)
        self.assertEqual(report["traces"]["completed_trees"], 2)

    def test_eager_tasks_and_cancelled_tasks_keep_results_errors_and_release_context(self):
        runtime, _, capture, app, monitor = self.monitored("""
async def eager():
    return 11
async def cancelled():
    await asyncio.sleep(10)
async def root():
    loop = asyncio.get_running_loop()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        value = await asyncio.create_task(eager())
        task = asyncio.create_task(cancelled())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return value
    finally:
        loop.set_task_factory(None)
""", task_policy())
        self.assertEqual(asyncio.run(app["root"]()), 11)
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        root = next(span for span in spans if span.name.endswith(".root"))
        self.assertEqual(len(spans), 3)
        self.assertTrue(all(span.parent_span_id == root.span_id for span in spans if span != root))
        self.assertEqual(sum(span.status.code == 2 for span in spans), 1)
        self.assertEqual(report["traces"]["losses"], {})

    def test_failed_task_factory_and_cancellation_before_start_do_not_retain_context(self):
        runtime, _, capture, app, monitor = self.monitored("""
async def child():
    return 3
factory_error = ValueError("factory failure")
async def root():
    loop = asyncio.get_running_loop()
    coroutine = child()
    def factory(loop, coroutine, **kwargs):
        raise factory_error
    loop.set_task_factory(factory)
    try:
        try:
            asyncio.create_task(coroutine)
        except ValueError as error:
            assert error is factory_error
    finally:
        coroutine.close()
        loop.set_task_factory(None)
    task = asyncio.create_task(child())
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return 9
""", task_policy())
        self.assertEqual(asyncio.run(app["root"]()), 9)
        report = self.finish(runtime, monitor)
        self.assertEqual(report["traces"]["losses"], {})
        self.assertEqual(report["traces"]["active_trees"], 0)
        self.assertTrue(any(span.name.endswith(".root") for span in capture.spans()))

    def test_caught_cancellation_restores_worker_context_before_new_submission(self):
        runtime, _, capture, app, monitor = self.monitored("""
async def grandchild():
    return 5
async def child():
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        return asyncio.create_task(grandchild())
async def root():
    return asyncio.create_task(child())
""", task_policy())
        async def exercise():
            child = await app["root"]()
            await asyncio.sleep(0)
            child.cancel()
            self.assertEqual(await (await child), 5)
        asyncio.run(exercise())
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        root = next(span for span in spans if span.name.endswith(".root"))
        child = next(span for span in spans if span.name.endswith(".child"))
        grandchild = next(span for span in spans if span.name.endswith(".grandchild"))
        self.assertEqual(child.parent_span_id, root.span_id)
        self.assertEqual(grandchild.parent_span_id, child.span_id)
        self.assertEqual(child.status.code, 0)
        self.assertEqual(report["traces"]["losses"], {})

    def test_unsampled_parent_suppresses_tasks_without_resampling(self):
        policy = task_policy()
        policy["traces"]["root_sample_ratio"] = 0
        runtime, _, capture, app, monitor = self.monitored("""
async def child():
    await asyncio.sleep(0)
    return 5
async def root():
    return await asyncio.gather(child(), child())
""", policy)
        self.assertEqual(asyncio.run(app["root"]()), [5, 5])
        report = self.finish(runtime, monitor)
        self.assertEqual(capture.spans(), [])
        self.assertEqual(report["traces"]["sampled_out_roots"], 1)
        self.assertEqual(report["traces"]["losses"], {})

    def test_context_capacity_invalidates_whole_tree_then_slots_are_reusable(self):
        policy = task_policy()
        policy["runtime"]["max_active_calls"] = 2
        runtime, _, capture, app, monitor = self.monitored("""
async def child():
    await asyncio.sleep(0)
    return 5
async def overloaded():
    return [asyncio.create_task(child()) for _ in range(3)]
async def healthy():
    return 9
""", policy)
        async def exercise():
            tasks = await app["overloaded"]()
            self.assertEqual(await asyncio.gather(*tasks), [5, 5, 5])
            await asyncio.sleep(0)
            self.assertEqual(await app["healthy"](), 9)
        asyncio.run(exercise())
        report = self.finish(runtime, monitor)
        self.assertEqual([span.name for span in capture.spans()], ["trace_fixture.healthy"])
        self.assertEqual(report["traces"]["losses"]["context_capacity"], 1)
        self.assertEqual(report["traces"]["pending_contexts"], 0)
        self.assertEqual(report["traces"]["active_trees"], 0)

    def test_completed_task_context_does_not_retain_application_payload_or_task(self):
        runtime, _, capture, app, monitor = self.monitored("""
async def child(payload):
    await asyncio.sleep(0)
    return 8
async def root(payload):
    return asyncio.create_task(child(payload))
""", task_policy())
        class Payload:
            pass
        async def exercise():
            payload = Payload()
            payload_ref = weakref.ref(payload)
            task = await app["root"](payload)
            task_ref = weakref.ref(task)
            self.assertEqual(await task, 8)
            del payload, task
            await asyncio.sleep(0)
            gc.collect()
            self.assertIsNone(payload_ref())
            self.assertIsNone(task_ref())
        asyncio.run(exercise())
        report = self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 2)
        self.assertEqual(report["traces"]["pending_contexts"], 0)

    def test_selection_capacity_in_detached_task_invalidates_inherited_tree(self):
        policy = task_policy()
        policy["runtime"]["max_functions"] = 1
        runtime, _, capture, app, monitor = self.monitored("""
async def child():
    return 4
async def root():
    return asyncio.create_task(child())
""", policy)
        async def exercise():
            self.assertEqual(await (await app["root"]()), 4)
        asyncio.run(exercise())
        report = self.finish(runtime, monitor)
        self.assertEqual(capture.spans(), [])
        self.assertEqual(report["traces"]["losses"]["function_capacity"], 1)
        self.assertEqual(report["traces"]["pending_contexts"], 0)

    def test_cli_example_preserves_source_results_and_exports_causal_task_trees(self):
        cli = ROOT / "target/debug/quux-otelc"
        if not cli.exists():
            self.skipTest("CLI is built by the native product gate")
        with receiver() as (endpoint, requests), tempfile.TemporaryDirectory(dir="/tmp") as directory:
            report = Path(directory) / "report.json"
            env = {key: value for key, value in os.environ.items() if not key.startswith(("OTEL_", "OTELC_"))}
            env.update(OTELC_PYTHON=sys.executable, OTELC_REPORT_PATH=str(report),
                       OTEL_EXPORTER_OTLP_ENDPOINT=endpoint)
            source = ROOT / "examples/apps/python_tasks_app.py"
            original = source.read_bytes()
            plain = subprocess.run([sys.executable, str(source)], capture_output=True, check=True, timeout=30)
            result = subprocess.run([str(cli), "--config", "examples/python-task-context.toml", "python", str(source)],
                                    cwd=ROOT, env=env, capture_output=True, timeout=30)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (plain.returncode, plain.stdout, plain.stderr))
            self.assertEqual(source.read_bytes(), original)
            status = json.loads(report.read_text())
            self.assertEqual(status["function_calls"], 8)
            self.assertEqual(status["export_loss"], 0)
            self.assertEqual(status["traces"]["pending_contexts"], 0)
            self.assertEqual(status["traces"]["losses"], {})
            spans = [span for path, _, body in requests if path == "/v1/traces"
                     for resource in ExportTraceServiceRequest.FromString(body).resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), 8)
            roots = [span for span in spans if not span.parent_span_id]
            self.assertEqual(len(roots), 3)
            self.assertEqual(len({span.trace_id for span in roots}), 3)
            for root in roots:
                children = [span for span in spans if span.parent_span_id == root.span_id]
                self.assertEqual(len(children), 1 if root.name.endswith(".detached") else 2)
                self.assertTrue(all(span.trace_id == root.trace_id for span in children))
                if root.name.endswith(".detached"):
                    self.assertGreater(children[0].end_time_unix_nano, root.end_time_unix_nano)
            doctor = subprocess.run([str(cli), "--config", "examples/python-task-context.toml",
                                     "--language", "python", "doctor"], cwd=ROOT, env=env,
                                    capture_output=True, timeout=30)
            self.assertEqual(doctor.returncode, 0, doctor.stderr.decode())
            self.assertIn(b"standard asyncio task context", doctor.stdout)

    def test_overlapping_monitors_fail_without_replacing_the_first_hook(self):
        first, _, _, _, first_monitor = self.monitored("def work(): return 1", task_policy())
        hook = asyncio.BaseEventLoop.create_task
        second, _, _ = self.runtime(task_policy())
        from quux_otelc_python.monitor import Monitor
        second_monitor = Monitor(second.plan, second)
        self.addCleanup(second_monitor.close)
        with self.assertRaisesRegex(ValueError, "already installed"):
            second_monitor.install()
        second_monitor.close()
        self.assertIs(asyncio.BaseEventLoop.create_task, hook)
        self.finish(first, first_monitor)

    def test_unqualified_detached_direct_task_reports_expired_context(self):
        runtime, _, capture, app, monitor = self.monitored("""
async def child():
    return 4
async def root():
    return asyncio.Task(child())
""", task_policy())
        async def exercise():
            self.assertEqual(await (await app["root"]()), 4)
        asyncio.run(exercise())
        report = self.finish(runtime, monitor)
        self.assertEqual(report["traces"]["losses"]["context_expired"], 1)
        self.assertEqual([span.name for span in capture.spans()], ["trace_fixture.root"])

    def test_incompatible_custom_future_keeps_result_and_reports_hook_loss(self):
        runtime, _, capture, app, monitor = self.monitored("""
class NoCallbacks(asyncio.Future):
    def add_done_callback(self, fn, *, context=None):
        raise RuntimeError("no callbacks")
async def child():
    return 4
async def root():
    loop = asyncio.get_running_loop()
    def factory(loop, coroutine, **kwargs):
        coroutine.close()
        future = NoCallbacks()
        future.set_result(12)
        return future
    loop.set_task_factory(factory)
    try:
        return loop.create_task(child()).result()
    finally:
        loop.set_task_factory(None)
""", task_policy())
        self.assertEqual(asyncio.run(app["root"]()), 12)
        report = self.finish(runtime, monitor)
        self.assertEqual(capture.spans(), [])
        self.assertEqual(report["traces"]["losses"]["context_hook"], 1)
        self.assertEqual(report["traces"]["pending_contexts"], 0)

    def test_shutdown_with_detached_work_drops_incomplete_tree_and_restores_hooks(self):
        original = asyncio.BaseEventLoop.create_task
        runtime, _, capture, app, monitor = self.monitored("""
async def child():
    await asyncio.sleep(10)
async def root():
    return asyncio.create_task(child())
""", task_policy())
        async def exercise():
            task = await app["root"]()
            await asyncio.sleep(0)
            report = self.finish(runtime, monitor)
            self.assertEqual(capture.spans(), [])
            self.assertEqual(report["traces"]["losses"]["incomplete"], 1)
            self.assertIs(asyncio.BaseEventLoop.create_task, original)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
