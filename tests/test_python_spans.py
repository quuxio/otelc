"""Qualify monitoring spans against original frames, SDK OTLP and loss boundaries."""
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from test_python_adapter import Capture, ROOT, plan
from quux_otelc_python.monitor import Monitor
from quux_otelc_python.telemetry import Runtime
from quux_otelc_python.traces import TraceSession
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceResponse
from opentelemetry.sdk.trace.export import SpanExportResult


class TraceCapture:
    def __init__(self):
        self.requests = []
        self.result = SpanExportResult.SUCCESS

    def export(self, spans):
        self.requests.append(encode_spans(spans))
        return self.result

    def shutdown(self):
        pass

    def spans(self):
        return [span for request in self.requests for resource in request.resource_spans
                for scope in resource.scope_spans for span in scope.spans]


def trace_plan():
    policy = plan()
    policy["source_matchers"] = {"include": [r"(?-u)^trace_fixture\.py$"], "exclude": []}
    policy["function_matchers"] = {"include": ["(?-u).*"], "exclude": []}
    policy["traces"] = {"enabled": True, "root_sample_ratio": 1.0,
                         "max_active_traces": 8, "max_spans_per_trace": 32}
    policy["trace_export"] = {"endpoint": "http://127.0.0.1:4318/v1/traces",
                              "protocol": "http/protobuf", "timeout_ms": 300}
    policy["export"]["max_queued_batches"] = 8
    return policy


@contextlib.contextmanager
def receiver(responses=None, gate=None):
    """A real OTLP transport boundary; record bytes without retaining app objects."""
    requests = []
    responses = list(responses or [(200, b"")])

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            if gate is not None:
                gate.wait(5)
            status, body = responses[min(len(requests) - 1, len(responses) - 1)]
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The producer has already enforced its deadline.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        if gate is not None:
            gate.set()
        server.shutdown()
        server.server_close()
        worker.join()


class PythonSpanHarness:
    def runtime(self, policy=None):
        metrics, traces = Capture(), TraceCapture()
        runtime = Runtime(policy or trace_plan(), metrics, traces)
        self.addCleanup(runtime.close)
        return runtime, metrics, traces

    def monitored(self, source, policy=None):
        runtime, metrics, traces = self.runtime(policy)
        namespace = {"asyncio": asyncio}
        exec(compile(source, str(ROOT / "trace_fixture.py"), "exec"), namespace)
        monitor = Monitor(runtime.plan, runtime)
        self.addCleanup(monitor.close)
        monitor.install()
        return runtime, metrics, traces, namespace, monitor

    def finish(self, runtime, monitor):
        monitor.close()
        return runtime.close()


class PythonSpanTests(PythonSpanHarness, unittest.TestCase):
    def test_recursive_trees_have_sdk_ids_parents_times_resource_scope_and_original_results(self):
        runtime, _, capture, app, monitor = self.monitored(
            "def recurse(depth):\n    return 0 if depth == 0 else 1 + recurse(depth - 1)\n")
        self.assertEqual(app["recurse"](3), 3)
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        self.assertEqual(len(spans), 4)
        self.assertEqual(report["function_calls"], 4)
        self.assertEqual(report["traces"]["completed_trees"], 1)
        for index, span in enumerate(spans):
            self.assertEqual(len(span.trace_id), 16)
            self.assertEqual(len(span.span_id), 8)
            self.assertNotEqual(span.span_id, bytes(8))
            self.assertEqual(span.trace_id, spans[0].trace_id)
            self.assertGreaterEqual(span.end_time_unix_nano, span.start_time_unix_nano)
            if index:
                self.assertEqual(span.parent_span_id, spans[index - 1].span_id)
                self.assertGreaterEqual(span.start_time_unix_nano, spans[index - 1].start_time_unix_nano)
                self.assertLessEqual(span.end_time_unix_nano, spans[index - 1].end_time_unix_nano)
            else:
                self.assertFalse(span.parent_span_id)
        resource = capture.requests[0].resource_spans[0]
        self.assertTrue(any(value.key == "service.name" for value in resource.resource.attributes))
        self.assertEqual(resource.scope_spans[0].scope.name, "quux.otelc")

    def test_direct_await_parenting_cancellation_and_independent_scheduled_tasks(self):
        runtime, _, capture, app, monitor = self.monitored("""
async def child():
    await asyncio.sleep(0)
    return 42
async def parent():
    return await child()
async def fork():
    return await asyncio.create_task(child())
async def cancelled(gate):
    await gate.wait()
"""
        )
        async def exercise():
            self.assertEqual(await app["parent"](), 42)
            self.assertEqual(await app["fork"](), 42)
            task = asyncio.create_task(app["cancelled"](asyncio.Event()))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            app["child"]().close()
        asyncio.run(exercise())
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        self.assertEqual(len(spans), 5)
        parent = next(span for span in spans if span.name.endswith(".parent"))
        nested = next(span for span in spans if span.parent_span_id == parent.span_id)
        self.assertTrue(nested.name.endswith(".child"))
        fork = next(span for span in spans if span.name.endswith(".fork"))
        independent = next(span for span in spans if span.name.endswith(".child") and span != nested)
        self.assertNotEqual(independent.trace_id, fork.trace_id)
        self.assertFalse(independent.parent_span_id)
        cancelled = next(span for span in spans if span.name.endswith(".cancelled"))
        self.assertEqual(cancelled.status.code, 2)
        self.assertTrue(any(attr.key == "otelc.cancelled" and attr.value.bool_value for attr in cancelled.attributes))
        self.assertEqual(report["traces"]["completed_trees"], 4)
        self.assertEqual(report["losses"]["incomplete"], 0)

    def test_generator_suspension_thread_movement_and_close_do_not_leak_parenting(self):
        runtime, _, capture, app, monitor = self.monitored("""
def child():
    return 7
def generator():
    yield child()
    yield child()
def other():
    return 9
"""
        )
        generator = app["generator"]()
        self.assertEqual(next(generator), 7)
        self.assertEqual(app["other"](), 9)
        values = []
        worker = threading.Thread(target=lambda: values.append(next(generator)))
        worker.start()
        worker.join()
        self.assertEqual(values, [7])
        generator.close()
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        root = next(span for span in spans if span.name.endswith(".generator"))
        children = [span for span in spans if span.name.endswith(".child")]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(span.parent_span_id == root.span_id for span in children))
        other = next(span for span in spans if span.name.endswith(".other"))
        self.assertNotEqual(other.trace_id, root.trace_id)
        self.assertEqual(root.status.code, 2)
        self.assertEqual(report["function_calls"], 4)

    def test_original_exception_payload_and_caught_exception_status(self):
        runtime, _, capture, app, monitor = self.monitored("""
def caught():
    try:
        raise ValueError('caught')
    except ValueError:
        return 5
def escape(error):
    raise error
"""
        )
        self.assertEqual(app["caught"](), 5)
        error = ValueError("original payload")
        with self.assertRaises(ValueError) as raised:
            app["escape"](error)
        self.assertIs(raised.exception, error)
        self.finish(runtime, monitor)
        spans = capture.spans()
        self.assertEqual([span.status.code for span in spans], [0, 2])
        self.assertNotIn("original payload", str(capture.requests))

    def test_metrics_admission_is_sticky_and_sampling_zero_does_not_report_incomplete_loss(self):
        policy = trace_plan()
        policy["metrics"]["enabled"] = False
        runtime, _, capture = self.runtime(policy)
        runtime.enter(1, "root")
        runtime.enabled = True
        runtime.enter(2, "child", 1)
        runtime.exit(2, False)
        runtime.exit(1, False)
        report = runtime.close()
        self.assertEqual(report["function_calls"], 1)
        self.assertEqual(len(capture.spans()), 2)
        policy = trace_plan()
        policy["metrics"]["enabled"] = False
        policy["traces"]["root_sample_ratio"] = 0
        runtime, _, capture = self.runtime(policy)
        runtime.enter(1, "root")
        runtime.enter(2, "child", 1)
        report = runtime.close()
        self.assertFalse(capture.spans())
        self.assertEqual(report["traces"]["sampled_out_roots"], 1)
        self.assertEqual(report["losses"]["incomplete"], 0)
        self.assertFalse(report["traces"]["losses"])

    def test_shared_function_active_and_span_limits_discard_whole_trees(self):
        for setting, reason in (("max_functions", "function_capacity"), ("max_active_calls", "active_call_capacity"), ("max_spans_per_trace", "span_capacity")):
            with self.subTest(setting=setting):
                policy = trace_plan()
                policy["traces" if setting == "max_spans_per_trace" else "runtime"][setting] = 1
                runtime, _, capture = self.runtime(policy)
                runtime.enter(1, "root")
                runtime.enter(2, "child", 1)
                runtime.exit(2, False)
                runtime.exit(1, False)
                report = runtime.close()
                self.assertFalse(capture.spans())
                self.assertEqual(report["traces"]["losses"][reason], 1)

    def test_trace_queue_capacity_export_failure_and_incomplete_shutdown_are_visible(self):
        policy = trace_plan()
        policy["traces"]["max_active_traces"] = 1
        policy["export"]["max_queued_batches"] = 1
        runtime, _, capture = self.runtime(policy)
        runtime.enter(1, "root")
        runtime.enter(2, "overflow")
        runtime.exit(2, False)
        runtime.exit(1, False)
        runtime.enter(3, "queue")
        runtime.exit(3, False)
        runtime.enter(4, "unfinished")
        capture.result = SpanExportResult.FAILURE
        report = runtime.close()
        self.assertEqual(len(capture.spans()), 1)
        self.assertEqual(report["traces"]["losses"], {"trace_capacity": 1, "queue_capacity": 1, "incomplete": 1})
        self.assertEqual(report["export_loss"], 1)

    def test_repeated_cache_rejection_invalidates_each_affected_tree(self):
        policy = trace_plan()
        policy["runtime"]["max_functions"] = 1
        runtime, _, capture, app, monitor = self.monitored("def root():\n    return child()\ndef child():\n    return 7\n", policy)
        self.assertEqual(app["root"](), 7)
        self.assertEqual(app["root"](), 7)
        report = self.finish(runtime, monitor)
        self.assertFalse(capture.spans())
        self.assertEqual(report["traces"]["losses"]["function_capacity"], 2)

    def test_large_completed_tree_is_discarded_before_sdk_encoding_and_loss_is_periodic(self):
        policy = trace_plan()
        policy["metrics"]["enabled"] = False
        policy["traces"].update(max_spans_per_trace=10000, max_active_traces=1)
        runtime, metrics, capture = self.runtime(policy)
        runtime.enter(1, "root")
        name = "x" * 1024
        for _ in range(8192):
            runtime.enter(2, name, 1)
            runtime.exit(2, False)
        runtime.exit(1, False)
        runtime.reader.collect()
        runtime.reader.collect()
        self.assertFalse(capture.requests)
        self.assertEqual(runtime.traces.losses["batch_bytes"], 1)
        self.assertTrue(any(point.value == 1 and point.attributes.get("reason") == "batch_bytes"
                            for batch in metrics.batches for resource in batch.resource_metrics
                            for scope in resource.scope_metrics for metric in scope.metrics
                            if metric.name == "otelc.trace.dropped_trees"
                            for point in metric.data.data_points))

    def test_deadline_and_transport_exceptions_are_bounded_and_redacted(self):
        import requests
        runtime, _, _ = self.runtime()
        session = TraceSession(runtime.traces)
        runtime.traces.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(TimeoutError, "deadline"):
            session.post("http://127.0.0.1/v1/traces")
        runtime.traces.deadline = time.monotonic() + 1
        with patch.object(requests.Session, "request", side_effect=requests.exceptions.InvalidHeader("private header value")):
            with self.assertRaisesRegex(requests.RequestException, "^OTLP trace request failed$"):
                session.post("http://127.0.0.1/v1/traces")

    def test_failed_trace_export_health_reaches_next_metrics_snapshot_without_new_calls(self):
        runtime, metrics, traces = self.runtime()
        traces.result = SpanExportResult.FAILURE
        runtime.enter(1, "one")
        runtime.exit(1, False)
        runtime.reader.collect()
        runtime.reader.collect()
        health = [point.value for batch in metrics.batches for resource in batch.resource_metrics
                  for scope in resource.scope_metrics for metric in scope.metrics
                  if metric.name == "otelc.export.dropped_batches"
                  for point in metric.data.data_points]
        self.assertEqual(health, [0, 1])
        self.assertEqual(len(traces.requests), 1)

    def test_forged_trace_settings_are_rejected_without_leaking_values(self):
        for group, key, value in (("traces", "root_sample_ratio", float("nan")), ("traces", "max_active_traces", 0), ("traces", "max_spans_per_trace", 65537), ("export", "max_queued_batches", 0), ("trace_export", "timeout_ms", 0), ("trace_export", "protocol", "grpc"), ("trace_export", "endpoint", "http://secret.example/private"), ("trace_export", "endpoint", "https://user:secret@example.invalid"), ("trace_export", "endpoint", "https://example.invalid?private=value")):
            policy = trace_plan()
            policy[group][key] = value
            with self.assertRaisesRegex(ValueError, "invalid resolved Python trace settings"):
                Runtime(policy, Capture(), TraceCapture())

    def test_typed_trace_acknowledgements_and_empty_signal_header_override(self):
        import requests
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_TRACES_HEADERS": "", "OTEL_EXPORTER_OTLP_HEADERS": "x-private=ignored"}, clear=True):
            runtime, _, _ = self.runtime()
        for body, status in ((b"", 200), (b"invalid", 422), (b"x" * 65537, 422), (ExportTraceServiceResponse(partial_success={"rejected_spans": 1}).SerializeToString(), 422)):
            response = requests.Response()
            response.status_code = 200
            response.raw = unittest.mock.Mock()
            response.raw.read.return_value = body
            with patch.object(requests.Session, "request", return_value=response) as posted:
                result = TraceSession(runtime.traces).post("http://127.0.0.1/v1/traces", timeout=1)
            self.assertEqual(result.status_code, status)
            self.assertEqual(result.content, b"")
            self.assertNotIn("x-private", posted.call_args.kwargs["headers"])
            self.assertFalse(posted.call_args.kwargs["allow_redirects"])

    def test_real_sdk_http_retry_and_permanent_or_partial_rejection(self):
        partial = ExportTraceServiceResponse(partial_success={"rejected_spans": 1}).SerializeToString()
        for responses, expected, loss in (([(503, b""), (200, b"")], 2, 0), ([(401, b"private backend response")], 1, 1), ([(200, partial)], 1, 1), ([(202, b"")], 1, 1), ([(302, b"")], 1, 1)):
            with self.subTest(responses=responses), receiver(responses) as (endpoint, requests):
                policy = trace_plan()
                policy["trace_export"].update(endpoint=endpoint + "/custom/traces", timeout_ms=5000)
                policy["runtime"]["shutdown_timeout_ms"] = 5000
                with patch.dict(os.environ, {}, clear=True):
                    runtime = Runtime(policy, Capture())
                self.addCleanup(runtime.close)
                runtime.enter(1, "original")
                runtime.exit(1, False)
                runtime.reader.collect()
                report = runtime.close()
                self.assertEqual(len(requests), expected)
                self.assertTrue(all(path == "/custom/traces" for path, _, _ in requests))
                self.assertTrue(all(body == requests[0][2] for _, _, body in requests))
                self.assertEqual(report["export_loss"], loss)
                spans = ExportTraceServiceRequest.FromString(requests[0][2]).resource_spans[0].scope_spans[0].spans
                self.assertEqual([span.name for span in spans], ["original"])

    def test_shared_ack_guard_also_reaches_real_sdk_metric_transport(self):
        body = ExportMetricsServiceResponse(partial_success={"rejected_data_points": 1}).SerializeToString()
        with receiver([(200, body)]) as (endpoint, requests):
            policy = plan()
            policy["metrics_endpoint"] = endpoint + "/v1/metrics"
            with patch.dict(os.environ, {}, clear=True):
                runtime = Runtime(policy)
            self.addCleanup(runtime.close)
            runtime.enter(1, "original")
            runtime.exit(1, False)
            runtime.reader.collect()
            self.assertEqual(runtime.export_loss, 1)
            self.assertEqual(len(requests), 1)
            self.assertTrue(ExportMetricsServiceRequest.FromString(requests[0][2]).resource_metrics)
            runtime.close()

    def test_real_stalled_transport_respects_global_shutdown_budget(self):
        gate = threading.Event()
        with receiver(gate=gate) as (endpoint, requests):
            policy = trace_plan()
            policy["trace_export"].update(endpoint=endpoint + "/v1/traces", timeout_ms=5000)
            policy["runtime"]["shutdown_timeout_ms"] = 120
            with patch.dict(os.environ, {}, clear=True):
                runtime = Runtime(policy, Capture())
            self.addCleanup(runtime.close)
            runtime.enter(1, "original")
            runtime.exit(1, False)
            started = time.monotonic()
            report = runtime.close()
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(requests)
            if report["export_finished"]:
                self.assertEqual(report["export_loss"], 1)
            else:
                self.assertTrue(runtime.reader._daemon_thread.is_alive())

    def test_cli_unchanged_and_annotated_apps_export_separate_typed_signals(self):
        cli = ROOT / "target/debug/quux-otelc"
        if not cli.exists():
            self.skipTest("build CLI before running Python product tests")
        with receiver() as (endpoint, requests), tempfile.TemporaryDirectory(dir="/tmp") as directory:
            directory = Path(directory)
            config, report = directory / "policy.toml", directory / "report.json"
            env = {key: value for key, value in os.environ.items() if not key.startswith(("OTEL_", "OTELC_"))}
            env.update(OTELC_PYTHON=sys.executable, OTELC_REPORT_PATH=str(report),
                       OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=endpoint + "/custom/metrics",
                       OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=endpoint + "/custom/traces",
                       OTEL_EXPORTER_OTLP_HEADERS="x-test=generic",
                       OTEL_EXPORTER_OTLP_TRACES_HEADERS="")
            policies = (("python_trace_app.py", (ROOT / "examples/python-traces.toml").read_text(), 9),
                        ("python_annotated.py", (ROOT / "examples/python.toml").read_text().replace('include = ["examples.apps.python_app.*", "examples.apps.python_annotated.configured"]', 'include = []') + '\n[traces]\nenabled = true\nroot_sample_ratio = 1.0\n', 1))
            for name, policy, count in policies:
                with self.subTest(name=name):
                    requests.clear()
                    config.write_text(policy)
                    source = ROOT / "examples/apps" / name
                    original = source.read_bytes()
                    plain = subprocess.run([sys.executable, str(source)], capture_output=True, check=True)
                    instrumented = subprocess.run([str(cli), "--config", str(config), "python", str(source)], cwd=ROOT, env=env, capture_output=True, timeout=30)
                    self.assertEqual(instrumented.returncode, plain.returncode, instrumented.stderr.decode())
                    self.assertEqual(instrumented.stdout, plain.stdout)
                    self.assertEqual(instrumented.stderr, plain.stderr)
                    self.assertEqual(source.read_bytes(), original)
                    status = json.loads(report.read_text())
                    self.assertEqual(status["function_calls"], count)
                    self.assertEqual(status["export_loss"], 0)
                    self.assertFalse(status["traces"]["losses"])
                    spans, metric_batches = [], []
                    for path, headers, body in requests:
                        if path == "/custom/traces":
                            spans.extend(span for resource in ExportTraceServiceRequest.FromString(body).resource_spans for scope in resource.scope_spans for span in scope.spans)
                            self.assertNotIn("x-test", headers)
                        else:
                            self.assertEqual(path, "/custom/metrics")
                            self.assertEqual(headers["x-test"], "generic")
                            metric_batches.append(ExportMetricsServiceRequest.FromString(body))
                    self.assertEqual(len(spans), count)
                    self.assertTrue(metric_batches)
                    doctor = subprocess.run([str(cli), "--config", str(config), "--language", "python", "doctor"], env=env, capture_output=True, timeout=30)
                    self.assertEqual(doctor.returncode, 0, doctor.stderr.decode())
                    self.assertIn(b"metrics and function spans", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
