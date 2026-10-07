"""Validate benchmark orchestration independently of installed language compilers."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import benchmark_languages as benchmark


class Process:
    def __init__(self):
        self.pid = 7
        self.stdin, self.stdout = Mock(), Mock()
        self.stopped = False

    def wait(self, timeout=None):
        self.stopped = True
        return 0

    def poll(self):
        return 0 if self.stopped else None


class LanguageBenchmarkTests(unittest.TestCase):
    def test_node_async_uses_unchanged_app_and_selects_correct_function(self):
        for language, extension in (("javascript", "mjs"), ("typescript", "mts")):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as directory:
                root, output = self.fixture(directory)
                (root / f"examples/apps/{language}_async_latency.{extension}").write_text("unchanged async app")
                (root / f"examples/{language}.toml").write_text(f'[functions]\ninclude=["examples.apps.{language}_app.*"]\n[resource]\nservice_name="test"\n')
                samples = {key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}
                with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=lambda *a, **k: Process()) as launch, patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", side_effect=['{"pid":42}', 'v26.10.0']), patch.object(benchmark, "measure", return_value=samples):
                    report = benchmark.run(root, output, 10, 2, language, node_async=True)
                self.assertTrue(report["node_async"])
                self.assertIn(f"{language}_async_latency.{extension}", launch.call_args_list[0].args[0][-1])
                self.assertIn(f"examples.apps.{language}_async_latency.process_order", (output / "policy.toml").read_text())

    def test_rust_compiles_unchanged_baseline_and_runs_live_adapter(self):
        for rust_async, rust_closures in ((False, False), (True, False), (False, True)):
            with self.subTest(rust_async=rust_async, rust_closures=rust_closures):
                self.check_rust_benchmark(rust_async, rust_closures)

    def check_rust_benchmark(self, rust_async, rust_closures):
        with tempfile.TemporaryDirectory() as directory:
            root, output = self.fixture(directory)
            name = "rust_async_latency" if rust_async else "rust_closure_latency" if rust_closures else "rust_latency"
            (root / f"examples/apps/{name}.rs").write_text("unchanged Rust application")
            (root / "examples/rust.toml").write_text('[functions]\ninclude=["examples.apps.rust_app.*"]\n[resource]\nservice_name="test"\n')
            samples = {key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "run") as compile_app, patch.object(benchmark.subprocess, "Popen", side_effect=lambda *a, **k: Process()) as launch, patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", side_effect=['{"pid":42}', 'rustc 1.98.1']), patch.object(benchmark, "measure", return_value=samples):
                report = benchmark.run(root, output, 10, 2, "rust", rust_async=rust_async, rust_closures=rust_closures)
            self.assertEqual(report["language"], "rust")
            self.assertEqual(report["toolchain"], "rustc 1.98.1")
            self.assertIn("--edition=2024", compile_app.call_args.args[0])
            self.assertEqual(launch.call_args_list[0].args[0], [str(output.resolve() / "plain-rust")])
            self.assertIn("rust", launch.call_args_list[1].args[0])
            function = "main.<closure>@*" if rust_closures else "process_order"
            self.assertIn(f"examples.apps.{name}.{function}", (output / "policy.toml").read_text())
            self.assertEqual(report["rust_async"], rust_async)
            self.assertEqual(report["rust_closures"], rust_closures)

    def fixture(self, directory, valid=True):
        root = Path(directory)
        (root / "examples/apps").mkdir(parents=True)
        (root / "examples/apps/python_latency.py").write_text("original application")
        (root / "examples/python.toml").write_text('[functions]\ninclude=["examples.apps.python_app.*"]\n[resource]\nservice_name="test"\n[export]\nendpoint="http://127.0.0.1:4318"\n')
        output = root / "result"
        output.mkdir()
        report = {"export_finished": True, "export_loss": 0 if valid else 1, "losses": {}, "function_calls": 1020}
        (output / "runtime.json").write_text(json.dumps(report))
        return root, output

    def test_driver_records_real_application_identity_and_validates_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = self.fixture(directory)
            samples = {key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=lambda *a, **k: Process()), patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", return_value='{"pid":42}'), patch.object(benchmark, "measure", return_value=samples) as measure:
                report = benchmark.run(root, output, 10, 2, endpoint="http://127.0.0.1:1234")
                self.assertEqual(report["same_instrumented_pid"], 42)
                self.assertTrue(report["complete_telemetry"])
                self.assertEqual(measure.call_args.args[-1], 42)
                data = json.loads((output / "runtime.json").read_text())
                data["export_loss"] = 1
                (output / "runtime.json").write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "Incomplete telemetry"):
                    benchmark.run(root, output, 10, 2)
            with patch.object(benchmark.subprocess, "Popen", side_effect=lambda *a, **k: Process()), patch.object(benchmark, "read_line", return_value="wrong"), patch.object(benchmark.os, "killpg") as kill:
                with self.assertRaisesRegex(ValueError, "initialise"):
                    benchmark.run(root, output, 10, 2)
                kill.assert_called_once()

    def test_bounds_and_command_line(self):
        for language, rust_async in (("python", False), ("rust", True)):
            with self.assertRaisesRegex(ValueError, "Rust closure benchmark"):
                benchmark.run("unused", "unused", 1, 2, language, rust_async=rust_async, rust_closures=True)
        with self.assertRaisesRegex(ValueError, "requires language=javascript"):
            benchmark.run("unused", "unused", 1, 2, "rust", node_async=True)
        with self.assertRaisesRegex(ValueError, "requires language=rust"):
            benchmark.run("unused", "unused", 1, 2, "python", rust_async=True)
        for iterations, runs, language in ((0, 2, "python"), (1, 1, "python"), (1, 2, "unknown")):
            with self.assertRaises(ValueError):
                benchmark.run("unused", "unused", iterations, runs, language)
        with patch.object(benchmark.sys, "argv", ["benchmark", "--language", "python"]), patch.object(benchmark, "run", return_value={"summary": {"complete": True}}), contextlib.redirect_stdout(io.StringIO()) as output:
            benchmark.main()
        self.assertIn("complete", output.getvalue())

    def test_java_launch_uses_same_source_and_external_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = self.fixture(directory)
            (root / "examples/apps/JavaLatency.java").write_text("unchanged Java application")
            (root / "examples/java.toml").write_text('[functions]\ninclude=["examples.apps.JavaApp*.*"]\n[resource]\nservice_name="test"\n')
            samples = {key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=lambda *a, **k: Process()) as launch, patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", side_effect=['{"pid":42}', 'openjdk 21']), patch.object(benchmark, "measure", return_value=samples):
                report = benchmark.run(root, output, 10, 2, "java")
            self.assertEqual(report["language"], "java")
            self.assertEqual(report["toolchain"], "openjdk 21")
            self.assertIn("JavaLatency.java", launch.call_args_list[0].args[0][-1])
            self.assertIn("java", launch.call_args_list[1].args[0])
            self.assertIn("examples.apps.JavaLatency.process_order(*)", (output / "policy.toml").read_text())

    def test_go_baseline_runs_original_source_with_live_adapter_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = self.fixture(directory)
            (root / "examples/apps/go_latency.go").write_text("unchanged Go application")
            (root / "examples/go.toml").write_text('[functions]\ninclude=["examples.apps.go_app.*"]\n[resource]\nservice_name="test"\n')
            samples = {key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=lambda *a, **k: Process()) as launch, patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", side_effect=['{"pid":42}', 'go version go1.27.1']), patch.object(benchmark, "measure", return_value=samples):
                report = benchmark.run(root, output, 10, 2, "go")
            self.assertEqual(report["language"], "go")
            self.assertEqual(launch.call_args_list[0].args[0][1], "run")
            self.assertIn("examples.apps.go_latency.process_order", (output / "policy.toml").read_text())
