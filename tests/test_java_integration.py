"""Actual upstream-agent execution, decoded OTLP and same-process switching."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import select
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Callable
from typing import Any

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from otelc import control, java
from otelc.config import Policy


def wait_for_count(observed: Callable[[], int], expected: int, timeout: float = 5.) -> int:
    """Await actual decoded receipt, not the completion of a flush request."""
    deadline = time.monotonic() + timeout
    value = observed()
    while value < expected and time.monotonic() < deadline:
        time.sleep(.02)
        value = observed()
    return value


class Capture:
    MAX_RETAINED_BYTES = 64 * 1024 * 1024
    MAX_RETAINED_REQUESTS = 1024

    def __init__(self, record_directory: Path | None = None) -> None:
        self.record_directory = record_directory
        self._record_lock = threading.Lock()
        self._record_count = self._record_bytes = 0
        if record_directory is not None:
            record_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.metrics: list[ExportMetricsServiceRequest] = []
        self.traces: list[ExportTraceServiceRequest] = []
        self.metric_response_gate = threading.Event()
        self.metric_response_gate.set()
        self.metric_request_seen = threading.Event()
        self.trace_response_gate = threading.Event()
        self.trace_response_gate.set()
        self.trace_request_seen = threading.Event()
        self.trace_response_code = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def request_payload(self) -> bytes:
                """Accept the actual Java fixed-length and Node chunked SDK transports."""
                if self.headers.get("Transfer-Encoding") != "chunked":
                    return self.rfile.read(int(self.headers["Content-Length"]))
                payload = bytearray()
                while True:
                    size = int(self.rfile.readline(128).split(b";", 1)[0], 16)
                    if size == 0:
                        if self.rfile.readline(128) != b"\r\n":
                            raise ValueError("unexpected OTLP chunk trailers")
                        return bytes(payload)
                    if size < 0 or len(payload) + size > 16 * 1024 * 1024:
                        raise ValueError("oversized OTLP test payload")
                    chunk = self.rfile.read(size)
                    if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                        raise ValueError("incomplete OTLP chunk")
                    payload.extend(chunk)

            def do_POST(self) -> None:
                response_code = 200
                payload = self.request_payload()
                owner.retain_request(self.path, self.headers.get("Content-Encoding", ""), payload)
                if self.headers.get("Content-Encoding") == "gzip":
                    payload = gzip.decompress(payload)
                if self.path.endswith("/metrics"):
                    message = ExportMetricsServiceRequest()
                    message.ParseFromString(payload)
                    owner.metrics.append(message)
                    owner.metric_request_seen.set()
                    owner.metric_response_gate.wait(timeout=10)
                elif self.path.endswith("/traces"):
                    message = ExportTraceServiceRequest()
                    message.ParseFromString(payload)
                    owner.traces.append(message)
                    owner.trace_request_seen.set()
                    owner.trace_response_gate.wait(timeout=10)
                    response_code = owner.trace_response_code
                else:
                    self.send_error(404)
                    return
                self.send_response(response_code)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_: object) -> None:
                """Keep the actual application stdout channel untouched."""

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def retain_request(self, path: str, encoding: str, payload: bytes) -> None:
        """Keep original HTTP entities before content decoding, without headers/tokens."""
        if self.record_directory is None:
            return
        with self._record_lock:
            if (self._record_count >= self.MAX_RETAINED_REQUESTS
                    or self._record_bytes + len(payload) > self.MAX_RETAINED_BYTES):
                raise ValueError('OTLP diagnostic capture budget exhausted')
            prefix = self.record_directory / f'otlp-{self._record_count:06d}'
            metadata = {'schema_version': 1, 'sequence': self._record_count,
                        'received_at_unix_ns': time.time_ns(), 'path': path,
                        'content_encoding': encoding, 'body_bytes': len(payload),
                        'body_sha256': hashlib.sha256(payload).hexdigest(),
                        'scope': 'received HTTP entity before content decoding; not response/SDK receipt'}
            for suffix, data in (('.http-body', payload),
                                 ('.json', (json.dumps(metadata, indent=2) + '\n').encode())):
                target = Path(str(prefix) + suffix)
                with target.open('xb') as stream:
                    stream.write(data)
                target.chmod(0o600)
            self._record_count += 1
            self._record_bytes += len(payload)

    def close(self) -> None:
        self.metric_response_gate.set()
        self.trace_response_gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def points(self, name: str) -> list[Any]:
        return [point for request in self.metrics for resource in request.resource_metrics
                for scope in resource.scope_metrics for metric in scope.metrics if metric.name == name
                for point in (metric.sum.data_points if metric.HasField("sum") else metric.histogram.data_points if metric.HasField("histogram") else metric.gauge.data_points)]

    def calls(self) -> int:
        return max((point.as_int for point in self.points("otelc.function.calls")), default=0)

    def lifetime_count(self, name: str, boundary: str, type_name: str) -> int:
        """Read the latest cumulative value for one lifecycle boundary/type."""
        values = []
        for point in self.points(name):
            attributes = {item.key: item.value.string_value for item in point.attributes}
            if attributes.get("boundary") == boundary and attributes.get("type") == type_name:
                values.append(point.as_int)
        return max(values, default=0)


def batch(process: subprocess.Popen[str], count: int | str) -> dict[str, Any]:
    if process.stdin is None or process.stdout is None:
        raise AssertionError("missing application pipes")
    process.stdin.write(f"{count}\n")
    process.stdin.flush()
    ready, _, _ = select.select([process.stdout], [], [], 20)
    if not ready:
        raise AssertionError("application batch timed out")
    return json.loads(process.stdout.readline())


class JavaIntegrationTests(unittest.TestCase):
    def test_receiver_wait_handles_an_export_already_in_flight(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(Policy.load(Path("examples/otelc.toml")).data)
        data["lifetimes"]["enabled"] = False
        data["traces"]["enabled"] = False
        data["export"].update(endpoint=f"http://127.0.0.1:{capture.server.server_port}",
                              interval_ms=2000, timeout_ms=10000)
        with tempfile.TemporaryDirectory(prefix="otelc-busy-", dir="/tmp") as directory:
            runtime = Path(directory)
            with subprocess.Popen(java.command(Policy.parse(data), runtime), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as process:
                try:
                    socket = runtime / "control.sock"
                    control.wait_ready(socket, process.pid)
                    capture.metric_response_gate.clear()
                    self.assertEqual(batch(process, 10)["checksum"], 41284864)
                    control.request(socket, "flush")
                    self.assertTrue(capture.metric_request_seen.wait(timeout=2))
                    self.assertEqual(capture.calls(), 10)
                    self.assertEqual(batch(process, 10)["checksum"], 41284864)
                    state = control.request(socket, "flush")
                    self.assertEqual(state["completed"], 20)
                    self.assertGreater(state["in_flight_exports"], 0)
                    self.assertEqual(capture.calls(), 10,
                                     "a flush reply can still refer to the older blocked export")
                    capture.metric_response_gate.set()
                    control.request(socket, "flush")
                    self.assertEqual(wait_for_count(capture.calls, 20), 20)
                    process.communicate("quit\n", timeout=15)
                    self.assertEqual(process.returncode, 0)
                finally:
                    capture.metric_response_gate.set()
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

    def test_official_agent_metrics_lifetimes_and_live_controls(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        source = Path("examples/java/BenchmarkApp.java")
        before = source.read_bytes()
        data = copy.deepcopy(Policy.load(Path("examples/otelc.toml")).data)
        data["export"]["endpoint"] = f"http://127.0.0.1:{capture.server.server_port}"
        data["traces"]["sample_ratio"] = 1.
        data["metrics"]["collection"] = False
        data["metrics"]["emission"] = False
        policy = Policy.parse(data)
        with tempfile.TemporaryDirectory(prefix="otelc-test-", dir="/tmp") as temporary:
            runtime = Path(temporary)
            with (runtime / "stderr.log").open("w+") as errors:
                environment = os.environ | {"OTEL_INSTRUMENTATION_METHODS_INCLUDE": "examples.BenchmarkApp[excluded]", "OTEL_METRICS_EXPORTER": "none"}
                process = subprocess.Popen(java.command(policy, runtime), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, env=environment)
                try:
                    socket = runtime / "control.sock"
                    first = control.wait_ready(socket, process.pid)
                    self.assertFalse(first["collection"])
                    self.assertFalse(first["emission"])
                    initial = batch(process, 10)
                    self.assertEqual(initial["checksum"], 41284864)
                    self.assertEqual(capture.calls(), 0)

                    enabled = control.request(socket, "on")
                    self.assertEqual(enabled["pid"], process.pid)
                    self.assertEqual(batch(process, 10)["checksum"], initial["checksum"])
                    self.assertEqual(batch(process, "throw"), {"exception": "IllegalArgumentException", "message": "negative seed"})
                    control.request(socket, "flush")
                    self.assertEqual(wait_for_count(capture.calls, 11), 11)

                    control.request(socket, "collection off")
                    batch(process, 10)
                    control.request(socket, "flush")
                    self.assertEqual(capture.calls(), 11)

                    control.request(socket, "emission off")
                    deadline = time.monotonic() + 3
                    while control.request(socket, "status")["in_flight_exports"] and time.monotonic() < deadline:
                        time.sleep(.01)
                    received = len(capture.metrics)
                    control.request(socket, "collection on")
                    batch(process, 10)
                    control.request(socket, "flush")
                    self.assertEqual(len(capture.metrics), received)

                    control.request(socket, "emission on")
                    control.request(socket, "flush")
                    self.assertEqual(wait_for_count(capture.calls, 21), 21)
                    self.assertEqual(control.request(socket, "status")["pid"], process.pid)
                    batch(process, "gc")
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        control.request(socket, "flush")
                        lifetime = capture.points("otelc.lifetime.completed")
                        boundaries = {attribute.value.string_value for point in lifetime for attribute in point.attributes if attribute.key == "boundary"}
                        gc_values = [point.as_int for point in lifetime if any(attribute.key == "boundary" and attribute.value.string_value == "observed_gc" for attribute in point.attributes)]
                        if {"resource", "observed_gc"} <= boundaries and max(gc_values, default=0) >= 20:
                            break
                        time.sleep(.05)
                    self.assertLessEqual({"resource", "observed_gc"}, boundaries, f"missing lifecycle telemetry: {boundaries}")
                    gc_completed = [point.as_int for point in lifetime if any(attribute.key == "boundary" and attribute.value.string_value == "observed_gc" for attribute in point.attributes)]
                    self.assertEqual(max(gc_completed), 20, "off-state constructions cannot become retroactive metric observations")
                    labels = {attribute.value.string_value for point in capture.points("otelc.function.calls") for attribute in point.attributes if attribute.key == "function"}
                    self.assertFalse(any("excluded" in label for label in labels))
                    self.assertNotIn("unknown", labels)

                    self.assertEqual(batch(process, "lifecycle"),
                                     {"construction_failed": True, "close_failed": True})
                    batch(process, "gc")
                    lifecycle_type = "examples.BenchmarkApp$LifecycleWorkItem"
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        control.request(socket, "flush")
                        if (capture.lifetime_count("otelc.lifetime.completed", "observed_gc", lifecycle_type) == 2
                                and capture.lifetime_count("otelc.lifetime.incomplete", "resource", lifecycle_type) == 1):
                            break
                        time.sleep(.05)
                    self.assertEqual(capture.lifetime_count("otelc.lifetime.admitted", "observed_gc", lifecycle_type), 2,
                                     "a throwing constructor cannot start a lifetime")
                    self.assertEqual(capture.lifetime_count("otelc.lifetime.completed", "observed_gc", lifecycle_type), 2)
                    self.assertEqual(capture.lifetime_count("otelc.lifetime.completed", "resource", lifecycle_type), 1,
                                     "failed and duplicate close cannot complete extra lifetimes")
                    self.assertEqual(capture.lifetime_count("otelc.lifetime.incomplete", "resource", lifecycle_type), 1)

                    control.request(socket, "off")
                    process.stdin.write("quit\n")
                    process.stdin.flush()
                    self.assertEqual(process.wait(timeout=10), 0)
                    self.assertTrue(capture.traces, "metrics switching must not disable trace export")
                    spans = [span for request in capture.traces for resource in request.resource_spans for scope in resource.scope_spans for span in scope.spans]
                    self.assertEqual(sum("workload" in span.name for span in spans), 41,
                                     "function spans must include calls made while metric collection/emission were off")
                    lifetime_spans = [span for span in spans if span.name.endswith(" lifetime")]
                    fixture_spans = [span for span in lifetime_spans if span.name == lifecycle_type + " lifetime"]
                    self.assertEqual(len(fixture_spans), 3, "two observed collections and one successful release")
                    for span in lifetime_spans:
                        attributes = {item.key: item.value.string_value for item in span.attributes}
                        self.assertIn(attributes["boundary"], {"observed_gc", "resource"})
                        self.assertGreaterEqual(span.end_time_unix_nano, span.start_time_unix_nano)
                        self.assertFalse(span.parent_span_id, "lifetimes use links instead of extending a completed parent")
                    workload_ids = {span.span_id for span in spans if "workload" in span.name}
                    self.assertTrue(any(link.span_id in workload_ids for span in lifetime_spans for link in span.links),
                                    "construction within a function must link to that function's span")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    if process.stdin:
                        process.stdin.close()
                    if process.stdout:
                        process.stdout.close()
                    errors.seek(0)
                    diagnostics = errors.read()
                    if "ERROR" in diagnostics:
                        self.fail(diagnostics[-6000:])
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
