"""Real JDK HTTP propagation between unchanged client/server JVMs, using the official agent."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from otelc import control, java, prepared_java
from otelc.config import Policy
from test_java_context import calls, command, metric_points
from test_java_integration import Capture, wait_for_count
from test_prepared_java import binding


def stop(process: subprocess.Popen[str]) -> None:
    """Retire a failed fixture before its Popen context waits on it."""
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)


def histogram_count(capture: Capture, name: str) -> int:
    """Count the latest typed-attribute SDK histogram series, without adding snapshots."""
    latest = {}
    for key, point in metric_points(capture, name, "histogram"):
        latest[key] = max(latest.get(key, 0), point.count)
    return sum(latest.values())


class Downstream:
    def __init__(self) -> None:
        self.received = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.received.append((self.headers.get("traceparent"), self.headers.get("baggage")))
                self.send_response(200)
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(b"8")

            def log_message(self, *_: object) -> None:
                """Keep receiver diagnostics out of ordinary application channels."""

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)


class JavaHttpContextTests(unittest.TestCase):
    def test_http_metrics_and_live_switches_work_without_trace_emission(self) -> None:
        self.exercise(True, traces=False)

    def test_official_client_server_carriers_parents_sampling_and_live_metrics(self) -> None:
        for http in (False, True):
            with self.subTest(http=http):
                self.exercise(http)

    def exercise(self, http: bool, *, traces: bool = True) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        downstream = Downstream()
        self.addCleanup(downstream.close)
        with tempfile.TemporaryDirectory(prefix="otelc-http-", dir="/tmp") as directory:
            root = Path(directory)
            source = java.ROOT / "examples/java/HttpContextApp.java"
            classes = root / "classes"
            classes.mkdir()
            helper = root / "HeaderReadGuard.java"
            helper.write_text("package io.quux.otelc.http; public class HeaderReadGuard {}")
            subprocess.run([java.executable("javac"), "-d", str(classes), str(source), str(helper)], check=True)
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "HttpContextApp", "classpath": ["classes"],
                "bindings": {"HttpContextApp": binding(source, classes / "HttpContextApp.class", "examples/java/HttpContextApp.java")}}))
            application = prepared_java.PreparedApplication.load(manifest)
            policy = Policy.parse({"schema_version": 3, "languages": ["java"],
                "sources": {"include": ["examples/**"]},
                "functions": {"include": ["HttpContextApp.request(*)", "HttpContextApp.handle(*)"]},
                "metrics": {"collection": False, "emission": False},
                "traces": {"enabled": traces, "sample_ratio": 1.0}, "context": {"http": http},
                "export": {"endpoint": f"http://127.0.0.1:{capture.server.server_port}"}})
            outcomes = []
            audits = []
            for plain in (True, False):
                lane = root / ("plain" if plain else "instrumented")
                lane.mkdir(mode=0o700)
                audit = lane / "requests.txt"
                launches = []
                with ExitStack() as stack:
                    def launch(role: str, argument: str) -> tuple[subprocess.Popen[str], Path]:
                        runtime = lane / role
                        runtime.mkdir(mode=0o700)
                        prepared = prepared_java.prepare(application, policy, runtime, plain=plain,
                            diagnostic_profile=True, arguments=(role, argument,
                                f"http://127.0.0.1:{downstream.server.server_port}/sink"))
                        process = stack.enter_context(subprocess.Popen(prepared.argv, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                        stack.callback(stop, process)
                        launches.append(prepared)
                        return process, runtime

                    server, server_runtime = launch("server", str(audit))
                    ready = command(server, None)
                    self.assertTrue(ready.startswith("ready:"), ready)
                    endpoint = "http://127.0.0.1:" + ready.split(":")[1]
                    client, client_runtime = launch("client", endpoint)
                    self.assertEqual(command(client, None), "ready")
                    if not plain:
                        control.wait_ready(server_runtime / "control.sock", server.pid)
                        control.wait_ready(client_runtime / "control.sock", client.pid)
                    values = []
                    for index, (collection, emission) in enumerate(((False, False), (True, False), (False, True), (True, True))):
                        if not plain:
                            for process, runtime in ((server, server_runtime), (client, client_runtime)):
                                control.request(runtime / "control.sock", "collection " + ("on" if collection else "off"))
                                status = control.request(runtime / "control.sock", "emission " + ("on" if emission else "off"))
                                self.assertEqual((status["pid"], status["collection"], status["emission"]),
                                                 (process.pid, collection, emission))
                        values.extend(command(client, path) for path in ("/ok", "/error"))
                        for sampled in (True, False):
                            trace = f"{index + (1 if sampled else 101):032x}"
                            parent = f"{index + 1:016x}"
                            request = urllib.request.Request(endpoint + ("/sampled" if sampled else "/unsampled"),
                                headers={"traceparent": f"00-{trace}-{parent}-0{int(sampled)}", "baggage": "session=ordinary"})
                            with urllib.request.urlopen(request, timeout=10) as response:
                                self.assertEqual((response.status, response.read()), (200, b"8"))
                        relay = urllib.request.Request(endpoint + "/relay", headers={
                            "traceparent": f"00-{index + 201:032x}-{index + 1:016x}-01", "baggage": "session=ordinary"})
                        with urllib.request.urlopen(relay, timeout=10) as response:
                            self.assertEqual((response.status, response.read()), (200, b"8"))
                        self.assertEqual(command(server, "barrier"), "idle")
                    if not plain:
                        for runtime in (server_runtime, client_runtime):
                            control.request(runtime / "control.sock", "flush")
                        self.assertEqual(wait_for_count(lambda: calls(capture), 16), 16)
                    for process in (client, server):
                        stdout, stderr = process.communicate("quit\n", timeout=15)
                        self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))
                    for prepared in launches:
                        prepared.verify_inputs()
                    outcomes.append(values)
                    audits.append([line.split("|") for line in audit.read_text().splitlines()])
            self.assertEqual(outcomes[0], ["200:8:-:true:false:true:true:true", "500:bad:-:true:false:true:true:true"] * 4)
            self.assertEqual(outcomes[0], outcomes[1], "original request headers and business responses must be unchanged")
            self.assertEqual(len(audits[0]), 20)
            self.assertEqual(len(audits[1]), 20)
            self.assertEqual([row for row in audits[0] if row[0] not in ("/ok", "/error")],
                             [row for row in audits[1] if row[0] not in ("/ok", "/error")])
            self.assertTrue(all(row[1] == "null" for row in audits[0] if row[0] in ("/ok", "/error")))
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), (56 if http else 32) if traces else 0)
            clients = {span.span_id: span for span in spans if span.kind == 3}
            servers = [span for span in spans if span.kind == 2]
            self.assertEqual(len(clients), 12 if http and traces else 0)
            self.assertEqual(len(servers), 16 if http and traces else 0)
            self.assertEqual(histogram_count(capture, "http.client.request.duration"), 6 if http else 0)
            self.assertEqual(histogram_count(capture, "http.server.request.duration"), 10 if http else 0)
            linked = [span for span in servers if span.parent_span_id in clients]
            self.assertEqual(len(linked), 8 if http and traces else 0)
            for span in linked:
                self.assertEqual(span.trace_id, clients[span.parent_span_id].trace_id)
            if http:
                self.assertTrue(all(row[1].startswith("00-") for row in audits[1] if row[0] in ("/ok", "/error")))
                if traces:
                    for index in range(4):
                        sampled = [span for span in servers if span.trace_id.hex() == f"{index + 1:032x}"]
                        self.assertEqual(len(sampled), 1)
                        self.assertEqual(sampled[0].parent_span_id.hex(), f"{index + 1:016x}")
                        self.assertFalse(any(span.trace_id.hex() == f"{index + 101:032x}" for span in spans))
                self.assertEqual(len(downstream.received), 8)
                self.assertEqual(downstream.received[:4], [(None, None)] * 4)
                for index, (parent, baggage) in enumerate(downstream.received[4:]):
                    self.assertEqual(baggage, "session=ordinary")
                    self.assertEqual(parent.split("-")[1], f"{index + 201:032x}")
                    if traces:
                        self.assertIn(bytes.fromhex(parent.split("-")[2]), clients)
                    else:
                        self.assertEqual(parent.split('-')[3], '00')
            else:
                self.assertTrue(all(row[1] == "null" for row in audits[1] if row[0] in ("/ok", "/error")))
