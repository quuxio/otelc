"""Exercise total OTLP deadlines against real trickled HTTP responses."""
import contextlib
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import requests
from test_python_adapter import plan
from quux_otelc_python.telemetry import Runtime, Session
from quux_otelc_python.traces import TraceSession
from quux_otelc_python.deadlines import Adapter, Deadline, workers
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceResponse


@contextlib.contextmanager
def trickle_receiver(mode: str, tls: ssl.SSLContext | None = None, statuses: list[int] | None = None):
    """Keep each idle wait short while making the complete response take seconds."""
    stopped = threading.Event()
    received = []
    acknowledgement = ExportMetricsServiceResponse()
    acknowledgement.partial_success.error_message = "warning" * 12
    body = acknowledgement.SerializeToString()

    class Receiver(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            status = statuses[min(len(received) - 1, len(statuses) - 1)] if statuses else 200
            headers = f"HTTP/1.1 {status} Response\r\nContent-Length: {len(body)}\r\nRetry-After: 0\r\n\r\n".encode()
            if mode == "no_eof":
                headers = b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\n\r\n"
            try:
                if mode == "fast":
                    self.wfile.write(headers + body)
                    self.wfile.flush()
                elif mode == "headers":
                    for value in headers:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        if stopped.wait(0.01): return
                    self.wfile.write(body)
                else:
                    self.wfile.write(headers)
                    for value in body:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        if stopped.wait(0.01): return
                    if mode == "no_eof":
                        stopped.wait(3)
            except (OSError, ValueError):
                pass  # A deadline must disconnect an unfinished receiver.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        scheme = "https" if tls is not None else "http"
        yield f"{scheme}://127.0.0.1:{server.server_port}", received
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        worker.join()


class PythonDeadlineTests(unittest.TestCase):
    def test_transient_retries_reuse_serialised_bytes_and_are_finitely_bounded(self):
        for statuses, expected, count in (([503, 200], 200, 2), ([503], 422, 6)):
            with self.subTest(statuses=statuses), trickle_receiver("fast", statuses=statuses) as (endpoint, received), Session() as session:
                started = time.monotonic()
                response = session.post(endpoint, data=b"same-serialised-batch", timeout=0.2)
                self.assertLess(time.monotonic() - started, 0.4)
                self.assertEqual(response.status_code, expected)
                self.assertEqual(received, [b"same-serialised-batch"] * count)

    def test_stalled_tls_handshake_cannot_extend_the_connection_budget(self):
        gate = threading.Event()
        listener = socket.create_server(("127.0.0.1", 0))

        def stall():
            with listener.accept()[0] as connection:
                connection.recv(4096)
                gate.wait(2)

        worker = threading.Thread(target=stall, daemon=True)
        worker.start()
        try:
            with Session() as session:
                started = time.monotonic()
                with self.assertRaises(requests.RequestException):
                    session.post(f"https://127.0.0.1:{listener.getsockname()[1]}", data=b"", timeout=0.04)
                self.assertLess(time.monotonic() - started, 0.3)
        finally:
            gate.set()
            listener.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_late_dns_is_bounded_and_cannot_emit_a_request_after_timeout(self):
        original = socket.getaddrinfo
        gate = threading.Event()

        def delayed(host, *args, **kwargs):
            if host == "otelc-deadline.invalid":
                gate.wait(2)
                host = "127.0.0.1"
            return original(host, *args, **kwargs)

        with trickle_receiver("fast") as (endpoint, received), patch("socket.getaddrinfo", side_effect=delayed):
            try:
                with Session() as session:
                    session.trust_env = False
                    for _ in range(6):
                        started = time.monotonic()
                        with self.assertRaises(requests.RequestException):
                            session.post(endpoint.replace("127.0.0.1", "otelc-deadline.invalid"), data=b"", timeout=0.04)
                        self.assertLess(time.monotonic() - started, 0.3)
                self.assertEqual(sum(thread.name.startswith("otelc-http-dns-") for thread in threading.enumerate()), 2)
            finally:
                gate.set()
            _, resolver = workers()
            resolver.jobs.join()
            self.assertEqual(received, [])

    def test_tls_verified_trickled_responses_and_reused_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / "certificate.pem", Path(directory) / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                           check=True, capture_output=True, timeout=10)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            for mode in ("fast", "body", "headers", "no_eof"):
                with self.subTest(mode=mode), trickle_receiver(mode, tls) as (endpoint, received), Session() as session:
                    for _ in range(3 if mode == "fast" else 1):
                        started = time.monotonic()
                        try:
                            response = session.post(endpoint, data=b"tls-batch", timeout=0.1, verify=str(cert))
                        except requests.RequestException:
                            response = None
                        self.assertLess(time.monotonic() - started, 0.4)
                        self.assertTrue(response is not None and response.status_code == 200 if mode == "fast"
                                        else response is None or response.status_code != 200)
                    self.assertTrue(received)
                    self.assertTrue(all(body == b"tls-batch" for body in received))

    def test_private_pools_and_deadline_capacity_do_not_modify_application_transport(self):
        from urllib3 import poolmanager
        from urllib3.connectionpool import HTTPConnectionPool
        self.assertIs(poolmanager.pool_classes_by_scheme["http"], HTTPConnectionPool)
        supervisor, _ = workers()
        states = [Deadline(time.monotonic() + 5) for _ in range(64)]
        try:
            for state in states:
                supervisor.add(state)
            with self.assertRaisesRegex(requests.RequestException, "capacity"):
                supervisor.add(Deadline(time.monotonic() + 5))
        finally:
            for state in states:
                state.finish()
                state.cancel()
                supervisor.remove(state)
        adapter = Adapter()
        with self.assertRaisesRegex(requests.RequestException, "proxy"):
            adapter.proxy_manager_for("socks5://127.0.0.1:9")

    def test_http_proxy_uses_the_same_elapsed_deadline(self):
        for mode in ("fast", "headers", "body"):
            with self.subTest(mode=mode), trickle_receiver(mode) as (proxy, received), Session() as session:
                session.trust_env = False
                started = time.monotonic()
                try:
                    response = session.post("http://otelc-proxy.invalid/v1/metrics", data=b"proxy-batch",
                                            proxies={"http": proxy}, timeout=0.04)
                except requests.RequestException:
                    response = None
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertTrue(response is not None and response.status_code == 200 if mode == "fast"
                                else response is None or response.status_code != 200)
                self.assertEqual(received, [b"proxy-batch"])

    def test_retry_after_does_not_extend_deadline_and_invalid_values_use_backoff(self):
        self.assertEqual(Session.retry_after("3", 1), 3)
        self.assertEqual(Session.retry_after("invalid", 1), 1)
        self.assertEqual(Session.retry_after("Thu, 01 Jan 1970 00:00:00 GMT", 1), 0)
        self.assertEqual(Session.retry_after(None, 1), 1)
        for timeout in (float("inf"), float("nan"), -1, 0):
            with Session() as session, self.assertRaises(requests.Timeout):
                session.post("http://127.0.0.1:9", timeout=timeout)

    def test_metric_and_trace_trickled_body_headers_and_no_eof_share_total_budget(self):
        for signal in ("metrics", "traces"):
            for mode in ("body", "headers", "no_eof"):
                with self.subTest(signal=signal, mode=mode), trickle_receiver(mode) as (endpoint, _):
                    deadline = time.monotonic() + 0.04
                    store = SimpleNamespace(headers={}, deadline=deadline,
                                            runtime=SimpleNamespace(shutdown_deadline=None))
                    session = Session() if signal == "metrics" else TraceSession(store)
                    self.addCleanup(session.close)
                    started = time.monotonic()
                    try:
                        response = session.post(endpoint + "/v1/" + signal, data=b"", timeout=0.04)
                    except requests.RequestException:
                        response = None
                    elapsed = time.monotonic() - started
                    self.assertLess(elapsed, 0.3, f"{signal}/{mode}: {elapsed:.3f}s")
                    self.assertTrue(response is None or response.status_code != 200)

    def test_runtime_close_records_loss_within_the_remaining_shutdown_budget(self):
        with trickle_receiver("body") as (endpoint, _):
            policy = plan()
            policy["metrics_endpoint"] = endpoint + "/v1/metrics"
            policy["export"]["timeout_ms"] = 40
            policy["runtime"]["shutdown_timeout_ms"] = 60
            runtime = Runtime(policy)
            self.addCleanup(runtime.close)
            runtime.enter(1, "selected")
            runtime.exit(1, False)
            started = time.monotonic()
            report = runtime.close()
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertGreater(report["export_loss"], 0)
            self.assertTrue(report["export_finished"])
