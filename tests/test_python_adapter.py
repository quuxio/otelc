"""Exercise the real interpreter/SDK boundary, including cumulative OTLP decoding."""
import importlib.util
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "adapters/python"
sys.path.insert(0, str(ADAPTER))
from quux_otelc_python.monitor import Monitor, annotations
from quux_otelc_python.policy import Selection, source_name
from quux_otelc_python.telemetry import Runtime, Session, export_headers
import launch


def plan():
    return {"schema_version": 2, "language": "python", "execution_available": True,
            "source_matchers": {"include": ["(?-u)^examples/apps/.*\\.py$"], "exclude": []},
            "function_matchers": {"include": ["(?-u)^examples\\.apps\\.python_app\\..*$"], "exclude": ["(?-u)^.*\\.excluded$", "(?-u)^.*\\.main$", "(?-u)^.*\\.main\\.<locals>\\..*$", "(?-u)^.*\\.asynchronous$"]},
            "annotations": {"read_existing": False},
            "runtime": {"max_functions": 64, "max_active_calls": 32, "shutdown_timeout_ms": 1000, "control_socket": None},
            "metrics": {"enabled": True, "histogram_boundaries_seconds": [0.001, 1.0]},
            "resource": {"service_name": "python-test", "service_version": "1", "attributes": {}},
            "export": {"interval_ms": 60000, "timeout_ms": 300, "max_queued_batches": 1},
            "metrics_endpoint": "http://127.0.0.1:4318/v1/metrics"}


class Capture(MetricExporter):
    def __init__(self):
        super().__init__()
        self.batches = []
        self.result = MetricExportResult.SUCCESS

    def export(self, metrics_data, **kwargs):
        self.batches.append(metrics_data)
        return self.result

    def shutdown(self, **kwargs):
        pass  # In-memory exporter owns no resources.

    def force_flush(self, **kwargs):
        return True


class PythonAdapterTests(unittest.TestCase):
    def runtime(self, policy=None):
        capture = Capture()
        runtime = Runtime(policy or plan(), capture)
        self.addCleanup(runtime.close)
        return runtime, capture

    def test_resolved_regex_semantics_source_protection_and_headers(self):
        selection = Selection({"include": [r"(?-u)^a\[x\].*$"], "exclude": [r"(?-u)^.*no$"]})
        self.assertTrue(selection.accepts("a[x].yes"))
        self.assertTrue(selection.accepts("a[x].\nname"))
        self.assertFalse(selection.accepts("ax.yes"))
        self.assertFalse(selection.accepts("a[x].no", True))
        self.assertTrue(selection.accepts("foreign", True))
        self.assertIsNone(source_name("/etc/example.py", ROOT))
        self.assertIsNone(source_name(str(ROOT / ".venv/example.py"), ROOT))
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_HEADERS": "api=value%20here"}):
            self.assertEqual(export_headers(), {"api": "value here"})
        for value in ("missing", "api=value%0Ainjection", "=value"):
            with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_METRICS_HEADERS": value}):
                with self.assertRaises(ValueError):
                    export_headers()

    def test_partial_invalid_and_bounded_otlp_responses(self):
        import requests
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceResponse
        for body, expected in ((b"", 200), (b"invalid", 422), (b"x" * 65537, 422), (ExportMetricsServiceResponse(partial_success={"rejected_data_points": 1}).SerializeToString(), 422)):
            response = requests.Response()
            response.status_code = 200
            response.raw = unittest.mock.Mock()
            response.raw.read.return_value = body
            with patch.object(requests.Session, "post", return_value=response) as posted:
                value = Session().post("http://127.0.0.1/v1/metrics")
            self.assertEqual(value.status_code, expected)
            self.assertEqual(value.content, b"")
            self.assertFalse(posted.call_args.kwargs["allow_redirects"])

    def test_limits_disabled_admission_and_export_failures(self):
        policy = plan()
        policy["runtime"].update(max_functions=1, max_active_calls=1)
        runtime, capture = self.runtime(policy)
        runtime.enabled = False
        runtime.enter(1, "first")
        self.assertFalse(runtime.pending)
        runtime.enabled = True
        runtime.enter(1, "first")
        runtime.enter(2, "second")
        runtime.enter(3, "first")
        runtime.enabled = False
        runtime.exit(1, True)
        runtime.exit(3, False)
        self.assertEqual(runtime.calls["first"]["count"], 1)
        self.assertEqual(runtime.calls["first"]["unwinds"], 1)
        self.assertEqual(runtime.loss["function_capacity"], 1)
        self.assertEqual(runtime.loss["active_call_capacity"], 1)
        capture.result = MetricExportResult.FAILURE
        runtime.reader.collect()
        self.assertEqual(runtime.export_loss, 1)
        runtime.reader.collect()
        self.assertEqual(len(capture.batches), 2)
        capture.result = MetricExportResult.SUCCESS
        runtime.reader.collect()
        runtime.reader.collect()
        self.assertEqual(len(capture.batches), 3)
        runtime.enabled = True
        runtime.enter(5, "first")
        runtime.enter(5, "first")
        self.assertEqual(runtime.loss["incomplete"], 1)
        runtime.exit(5, False)
        with patch.object(runtime.counter, "add", side_effect=ValueError):
            runtime.enter(6, "first")
            runtime.exit(6, False)
        self.assertEqual(runtime.loss["invalid"], 1)
        runtime.exporter.force_flush()

    def test_disabled_monitor_has_no_callbacks_after_inflight_completion(self):
        if not hasattr(sys, "monitoring"):
            self.skipTest("requires Python 3.12+")
        runtime, _ = self.runtime()
        monitor = Monitor(plan(), runtime)
        monitor.install()
        self.addCleanup(monitor.close)
        runtime.enter(1, "admitted")
        runtime.enabled = False
        events = sys.monitoring.get_events(monitor.tool)
        self.assertFalse(events & sys.monitoring.events.PY_START)
        self.assertTrue(events & sys.monitoring.events.PY_RETURN)
        runtime.exit(1, False)
        self.assertEqual(sys.monitoring.get_events(monitor.tool), 0)
        with patch.object(sys, "_getframe", side_effect=AssertionError("disabled frame lookup")):
            monitor.start(compile("pass", "a.py", "exec"), 0)
        with patch.object(runtime, "lock") as lock:
            runtime.enter(2, "disabled")
            lock.__enter__.assert_not_called()
        runtime.enabled = True
        self.assertTrue(sys.monitoring.get_events(monitor.tool) & sys.monitoring.events.PY_START)

    def test_enable_during_unobserved_coroutine_keeps_future_returns(self):
        import asyncio
        policy = plan()
        policy["metrics"]["enabled"] = False
        policy["source_matchers"] = {"include": ["(?-u).*"], "exclude": []}
        policy["function_matchers"] = {"include": ["(?-u)^review\\.target$"], "exclude": []}
        runtime, _ = self.runtime(policy)
        namespace = {}
        exec(compile("async def target(gate):\n    await gate.wait()\n    return 7\n", str(ROOT / "review.py"), "exec"), namespace)
        monitor = Monitor(policy, runtime)
        self.addCleanup(monitor.close)
        monitor.install()

        async def exercise():
            gate = asyncio.Event()
            first = asyncio.create_task(namespace["target"](gate))
            await asyncio.sleep(0)
            runtime.enabled = True
            gate.set()
            self.assertEqual(await first, 7)
            self.assertEqual(await namespace["target"](gate), 7)

        asyncio.run(exercise())
        self.assertEqual(runtime.calls["review.target"]["count"], 1)
        self.assertFalse(runtime.pending)

    def test_same_line_lambdas_have_distinct_function_identities(self):
        policy = plan()
        policy["source_matchers"] = {"include": ["(?-u).*"], "exclude": []}
        policy["function_matchers"] = {"include": ["(?-u).*"], "exclude": []}
        namespace = {}
        exec(compile("callbacks = [lambda: 1, lambda: 2]", str(ROOT / "review.py"), "exec"), namespace)
        monitor = Monitor(policy, None)
        names = [monitor.display_name(value.__code__) for value in namespace["callbacks"]]
        self.assertEqual(len(set(names)), 2, names)

    def test_annotations_and_monitor_tool_cleanup(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "a.py"
            path.write_text("# otelc.instrument\n# otelc.exclude\ndef f(): pass\n")
            self.assertEqual(annotations(path), {3: "otelc.exclude"})
            path.write_bytes(b"# coding: latin-1\n# caf\xe9\n# otelc.instrument\ndef f(): pass\n")
            self.assertEqual(annotations(path), {4: "otelc.instrument"})
            path.write_text("# otelc.unknown\ndef f(): pass\n")
            with self.assertRaises(ValueError):
                annotations(path)
        runtime, _ = self.runtime()
        monitor = Monitor(plan(), runtime)
        monitor.close()
        if not hasattr(sys, "monitoring"):
            return
        monitor.install()
        tool = monitor.tool
        monitor.close()
        self.assertIsNone(sys.monitoring.get_tool(tool))
        with patch.object(sys.monitoring, "get_tool", return_value="other"):
            with self.assertRaises(ValueError):
                monitor.install()

    def test_monitor_selection_annotations_caps_and_frame_tokens(self):
        if not hasattr(sys, "monitoring"):
            self.skipTest("requires Python 3.12+")
        policy = plan()
        policy["annotations"]["read_existing"] = True
        policy["function_matchers"]["include"] = []
        runtime, _ = self.runtime(policy)
        monitor = Monitor(policy, runtime)
        path = ROOT / "examples/apps/python_annotated.py"
        code = compile(path.read_text(), str(path), "exec")
        functions = {c.co_name: c for c in code.co_consts if hasattr(c, "co_name")}
        self.assertIsNone(monitor.display_name(code))
        self.assertIsNone(monitor.display_name(functions["excluded"]))
        self.assertIsNone(monitor.display_name(functions["configured"]))
        selected = functions["selected"]
        self.assertEqual(monitor.display_name(selected), "examples.apps.python_annotated.selected")
        frame = object()
        with patch.object(sys, "_getframe", return_value=frame):
            monitor.start(selected, 0)
            monitor.returned(selected, 0, None)
            monitor.start(selected, 0)
            monitor.unwound(selected, 0, ValueError())
        self.assertEqual(runtime.calls["examples.apps.python_annotated.selected"]["count"], 2)
        self.assertEqual(runtime.calls["examples.apps.python_annotated.selected"]["unwinds"], 1)
        self.assertIs(monitor.start(functions["excluded"], 0), sys.monitoring.DISABLE)
        self.assertIs(monitor.returned(functions["excluded"], 0, None), sys.monitoring.DISABLE)
        monitor.unwound(functions["excluded"], 0, ValueError())
        monitor.names.clear()
        policy["runtime"]["max_functions"] = 0
        self.assertIs(monitor.start(selected, 0), sys.monitoring.DISABLE)
        self.assertEqual(runtime.loss["function_capacity"], 1)

    def test_launcher_script_module_doctor_and_invalid_requests(self):
        if not hasattr(sys, "monitoring"):
            self.skipTest("requires Python 3.12+")
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan()))
            with patch.object(launch, "Runtime", side_effect=lambda policy: Runtime(policy, Capture())), patch.object(sys, "argv", []), patch.object(sys, "path", list(sys.path)), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(launch.main([str(path), "--doctor"]), 0)
                self.assertEqual(launch.main([str(path), "examples/apps/python_annotated.py"]), 0)
                self.assertEqual(launch.main([str(path), "--inspect", "examples/apps/python_app.py", "--json"]), 0)
                with self.assertRaises(ValueError):
                    launch.main([str(path), "--inspect"])
                self.assertEqual(launch.main([str(path), "-m", "examples.apps.python_app"]), 0)
                with self.assertRaises(ValueError):
                    launch.main([str(path), "-m"])
                with self.assertRaises(ValueError):
                    launch.main([])
                invalid = plan()
                invalid["execution_available"] = False
                path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    launch.main([str(path), "app.py"])
            self.assertIn("151", output.getvalue())

    def test_original_code_exception_async_generator_and_thread_semantics(self):
        if not hasattr(sys, "monitoring"):
            self.skipTest("requires Python 3.12+")
        runtime, capture = self.runtime()
        monitor = Monitor(plan(), runtime)
        spec = importlib.util.spec_from_file_location("python_app", ROOT / "examples/apps/python_app.py")
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        source = (ROOT / "examples/apps/python_app.py").read_bytes()
        monitor.install()
        try:
            app.main()
        finally:
            monitor.close()
        self.assertEqual((ROOT / "examples/apps/python_app.py").read_bytes(), source)
        counts = {k.rsplit(".", 1)[-1]: v for k, v in runtime.calls.items()}
        self.assertEqual(counts["process_order"]["count"], 8)
        self.assertEqual(counts["recursive"]["count"], 4)
        self.assertEqual(counts["throwing"]["unwinds"], 1)
        self.assertEqual(counts["caught"]["unwinds"], 0)
        self.assertEqual(counts["values"]["count"], 1)
        self.assertEqual(counts["delayed"]["count"], 2)
        self.assertEqual(counts["cancelled"]["unwinds"], 1)
        self.assertNotIn("excluded", counts)
        self.assertFalse(runtime.pending)
        self.assertEqual(sum(runtime.loss.values()), 0)
        runtime.reader.collect()
        metrics = capture.batches[0].resource_metrics[0].scope_metrics[0].metrics
        calls = next(m for m in metrics if m.name == "otelc.function.calls")
        self.assertEqual(sum(p.value for p in calls.data.data_points), 18)

    def control(self, path, command):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(path))
            connection.sendall(command)
            return json.loads(connection.recv(4096))

    def test_owner_control_inflight_and_occupied_path(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "metrics.sock"
            policy = plan()
            policy["runtime"]["control_socket"] = str(path)
            runtime, _ = self.runtime(policy)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            runtime.enter(1, "selected")
            self.assertFalse(self.control(path, b"disable\n")["metrics_enabled"])
            runtime.enter(2, "selected")
            self.control(path, b"enable\n")
            runtime.exit(1, False)
            runtime.exit(2, False)
            status = self.control(path, b"status\n")
            self.assertEqual(status["function_calls"], 1)
            self.assertEqual(status["pid"], os.getpid())
            self.assertIn("error", self.control(path, b"invalid\n"))
            self.assertIn("error", self.control(path, b"x" * 18 + b"\n"))
            self.assertIn("error", self.control(path, b"unterminated"))
            with self.assertRaises(OSError):
                Runtime(policy, Capture())
            runtime.close()
            self.assertFalse(path.exists())
            os.chmod(directory, 0o755)
            with self.assertRaises(ValueError):
                Runtime(policy, Capture())

    def test_cli_launch_otlp_unchanged_annotations_and_exit_status(self):
        cli = ROOT / "target/debug/quux-otelc"
        if not cli.exists() or not hasattr(sys, "monitoring"):
            self.skipTest("build CLI and use Python 3.12+")
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        batches = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                self.assert_path = self.path
                batches.append(ExportMetricsServiceRequest.FromString(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass  # Keep test HTTP traffic out of product output.

        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(dir="/tmp") as directory:
                config = Path(directory) / "policy.toml"
                config.write_text((ROOT / "examples/python.toml").read_text().replace("127.0.0.1:4318", f"127.0.0.1:{server.server_port}"))
                report = Path(directory) / "report.json"
                env = dict(os.environ, OTELC_PYTHON=sys.executable, OTELC_REPORT_PATH=str(report))
                for name, expected in (("python_app.py", 151), ("python_annotated.py", 30)):
                    source = ROOT / "examples/apps" / name
                    before = source.read_bytes()
                    plain = subprocess.run([sys.executable, str(source)], capture_output=True, check=True)
                    instrumented = subprocess.run([str(cli), "--config", str(config), "python", str(source)], cwd=ROOT, env=env, capture_output=True)
                    self.assertEqual(instrumented.returncode, 0, instrumented.stderr)
                    self.assertEqual(instrumented.stdout, plain.stdout)
                    self.assertEqual(int(plain.stdout), expected)
                    self.assertEqual(source.read_bytes(), before)
                    data = json.loads(report.read_text())
                    self.assertEqual(sum(data["losses"].values()) + data["export_loss"], 0)
                self.assertEqual(data["function_calls"], 2)
                self.assertEqual(set(data["functions"]), {"examples.apps.python_annotated.selected", "examples.apps.python_annotated.configured"})
                metric = next(m for m in batches[-1].resource_metrics[0].scope_metrics[0].metrics if m.name == "otelc.function.calls")
                self.assertEqual(sum(p.as_int for p in metric.sum.data_points), 2)
                from scripts import benchmark_languages
                with patch.object(benchmark_languages.subprocess, "Popen", wraps=subprocess.Popen):
                    result = benchmark_languages.run(ROOT, Path(directory) / "benchmark", 40, 2, endpoint=f"http://127.0.0.1:{server.server_port}")
                self.assertTrue(result["complete_telemetry"])
                self.assertEqual(result["runtime"]["function_calls"], 1080)
                for iterations, runs, language in ((0, 2, "python"), (1, 1, "python"), (1, 2, "unknown")):
                    with self.assertRaises(ValueError):
                        benchmark_languages.run(ROOT, Path(directory), iterations, runs, language)
                with patch.object(sys, "argv", ["benchmark_languages.py", "--language", "python"]), patch.object(benchmark_languages, "run", return_value=result), contextlib.redirect_stdout(io.StringIO()):
                    benchmark_languages.main()
                status = subprocess.run([str(cli), "--config", str(config), "--language", "python", "doctor"], env=env, capture_output=True)
                self.assertEqual(status.returncode, 0, status.stderr)
                inspection = subprocess.run([str(cli), "--config", str(config), "--language", "python", "inspect", "examples/apps/python_annotated.py", "--json"], env=env, cwd=ROOT, capture_output=True)
                self.assertEqual(inspection.returncode, 0, inspection.stderr)
                self.assertEqual(sum(v["selected"] for v in json.loads(inspection.stdout)["functions"]), 2)
                modules = Path(directory) / "modules"
                modules.mkdir()
                (modules / "telemetry.py").write_text("value = 17\n")
                (modules / "policy.py").write_text("value = 23\n")
                (modules / "app.py").write_text("import telemetry, policy\nprint(telemetry.value + policy.value)\n")
                user_modules = subprocess.run([str(cli), "--config", str(config), "python", "app.py"], env=env, cwd=modules, capture_output=True)
                self.assertEqual(user_modules.returncode, 0, user_modules.stderr)
                self.assertEqual(user_modules.stdout.strip(), b"40")
                bad = subprocess.run([str(cli), "--config", str(config), "python", "-m"], env=env, capture_output=True)
                self.assertNotEqual(bad.returncode, 0)
        finally:
            server.shutdown()
            server.server_close()
