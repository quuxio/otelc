"""Validate benchmark orchestration independently of installed language compilers."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import benchmark_languages as benchmark


class Process:
    def __init__(self, report_path=None, report=None):
        self.report_path, self.report = report_path, report
        self.pid = 7
        self.stdin, self.stdout = Mock(), Mock()
        self.stopped = False

    def wait(self, timeout=None):
        self.stopped = True
        if self.report_path is not None:
            self.report_path.write_text(json.dumps(self.report))
        return 0

    def poll(self):
        return 0 if self.stopped else None


class LanguageBenchmarkTests(unittest.TestCase):
    def test_reused_runtime_report_cannot_satisfy_a_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = self.fixture(directory)
            (output / "runtime.json").write_text(json.dumps(self.telemetry))
            (output / "report.json").write_text('{"complete_telemetry":true}')
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=lambda *a, **k: Process()), patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", return_value='{"pid":42}'), patch.object(benchmark, "measure", return_value={key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}):
                with self.assertRaises((FileNotFoundError, ValueError)):
                    benchmark.run(root, output, 10, 2)
            self.assertFalse((output / "report.json").exists())

    def test_runtime_report_must_match_the_control_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = self.fixture(directory)
            self.telemetry["pid"] = 99
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=self.launch), patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", return_value='{"pid":42}'), patch.object(benchmark, "measure", return_value={key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}):
                with self.assertRaisesRegex(ValueError, "another process"):
                    benchmark.run(root, output, 10, 2)

    def launch(self, command, **kwargs):
        instrumented = command[0].endswith("quux-otelc")
        return Process(Path(kwargs["env"]["OTELC_REPORT_PATH"]) if instrumented else None, self.telemetry)

    def build(self, command, **kwargs):
        if "--build" in command:
            bundle = Path(command[command.index("--output-dir") + 1])
            bundle.mkdir()
            (bundle / "build.json").write_text(json.dumps(self.metadata))
            kwargs["stdout"].write(b"compiler output")

    def test_node_async_uses_unchanged_app_and_selects_correct_function(self):
        for language, extension in (("javascript", "mjs"), ("typescript", "mts")):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as directory:
                root, output = self.fixture(directory)
                (root / f"examples/apps/{language}_async_latency.{extension}").write_text("unchanged async app")
                (root / f"examples/{language}.toml").write_text(f'[functions]\ninclude=["examples.apps.{language}_app.*"]\n[resource]\nservice_name="test"\n')
                samples = {key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}
                with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=self.launch) as launch, patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", side_effect=['{"pid":42}', 'v26.10.0']), patch.object(benchmark, "measure", return_value=samples):
                    report = benchmark.run(root, output, 10, 2, language, node_async=True)
                self.assertTrue(report["node_async"])
                self.assertIn(f"{language}_async_latency.{extension}", launch.call_args_list[0].args[0][-1])
                self.assertIn(f"examples.apps.{language}_async_latency.process_order", (output / "policy.toml").read_text())

    def test_native_typescript_benchmark_uses_same_compiler_in_both_lanes(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = self.fixture(directory)
            (root / "examples/apps/typescript_latency.mts").write_text("unchanged typed app")
            (root / "examples/typescript.toml").write_text('[functions]\ninclude=["examples.apps.typescript_app.*"]\n[resource]\nservice_name="test"\n[adapters.typescript]\nbackend = "source"\n')
            samples = {key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=self.launch) as launch, patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", side_effect=['{"pid":42}', 'v24.21.0']), patch.object(benchmark, "measure", return_value=samples):
                report = benchmark.run(root, output, 10, 2, "typescript", native_typescript=True)
            self.assertTrue(report["native_typescript"])
            self.assertIn('backend = "native"', (output / "policy.toml").read_text())
            self.assertEqual(len(launch.call_args_list), 2)
            self.assertTrue(all(call.kwargs['env']['OTELC_TYPESCRIPT_BACKEND'] == 'native' for call in launch.call_args_list))
        with self.assertRaisesRegex(ValueError, "requires language=typescript"):
            benchmark.run("unused", "unused", 1, 2, "python", native_typescript=True)

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
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "run", side_effect=self.build) as compile_app, patch.object(benchmark.subprocess, "Popen", side_effect=self.launch) as launch, patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", side_effect=['{"pid":42}', 'rustc 1.98.1']), patch.object(benchmark, "measure", return_value=samples):
                report = benchmark.run(root, output, 10, 2, "rust", rust_async=rust_async, rust_closures=rust_closures)
            self.assertEqual(report["language"], "rust")
            self.assertEqual(report["toolchain"], "rustc 1.98.1")
            self.assertEqual(compile_app.call_args.args[0], [self.metadata["compiler"]["program"], *self.metadata["baseline_flags"], str(root.resolve() / f"examples/apps/{name}.rs"), "-o", str(output.resolve() / "plain-rust")])
            self.assertEqual(launch.call_args_list[0].args[0], [str(output.resolve() / "plain-rust")])
            self.assertIn("--run", launch.call_args_list[1].args[0])
            self.assertIn("rust_build", report)
            self.assertEqual(report["rust_build"]["metadata"], self.metadata)
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
        self.telemetry = {"pid": 42, "export_finished": True, "export_loss": 0 if valid else 1, "losses": {}, "function_calls": 1020}
        self.metadata = {"schema_version": 3, "backend": "compiler", "compiler": {"program": "/qualified/stable/rustc"}, "baseline_flags": ["--edition=2024", "-Copt-level=1", "-g"]}
        return root, output

    def test_rust_llvm_baseline_uses_actual_bundle_compiler_flags_and_replay(self):
        flags = ["--edition=2024", "-O", "-g", "-Zmir-enable-passes=-Inline,-ForceInline", "-Ccodegen-units=1"]
        metadata = {"schema_version": 2, "backend": "llvm", "compiler": {"program": "/qualified/nightly/rustc"}, "baseline_flags": flags}
        with tempfile.TemporaryDirectory() as directory:
            root, output = self.fixture(directory)
            source = root / "examples/apps/rust_latency.rs"
            source.write_text("unchanged Rust application")
            (root / "examples/rust.toml").write_text('[functions]\ninclude=["examples.apps.rust_app.*"]\n[resource]\nservice_name="test"\n[adapters.rust]\nbackend = "compiler"\n')

            def build(command, **kwargs):
                self.assertNotIn("OTELC_RUSTC", kwargs["env"])
                if "--build" in command:
                    bundle = Path(command[command.index("--output-dir") + 1])
                    bundle.mkdir()
                    (bundle / "build.json").write_text(json.dumps(metadata))
                    kwargs["stdout"].write(b"build output")

            samples = {key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}
            with patch.dict(os.environ, {"OTELC_RUSTC": "/wrong/compiler"}), patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "run", side_effect=build) as compile_app, patch.object(benchmark.subprocess, "Popen", side_effect=self.launch) as launch, patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", side_effect=['{"pid":42}', 'qualified nightly']), patch.object(benchmark, "measure", return_value=samples):
                report = benchmark.run(root, output, 10, 2, "rust", rust_llvm=True)
            self.assertEqual(compile_app.call_args_list[1].args[0], ["/qualified/nightly/rustc", *flags, str(source.resolve()), "-o", str(output.resolve() / "plain-rust")])
            self.assertIn("--run", launch.call_args_list[1].args[0])
            self.assertNotIn(str(source), launch.call_args_list[1].args[0])
            self.assertIn('backend = "llvm"', (output / "policy.toml").read_text())
            self.assertTrue(report["rust_llvm"])
            self.assertEqual(report["rust_build"]["metadata"], metadata)
            self.assertEqual(source.read_text(), "unchanged Rust application")
            bundle = Path(report["rust_build"]["directory"])
            self.assertTrue(bundle.is_dir())
            self.assertEqual((bundle.parent / "build.stdout").read_bytes(), b"build output")

    def test_rust_llvm_refuses_other_languages_async_and_wrong_bundle_backend(self):
        for language, asynchronous in (("python", False), ("rust", True)):
            with self.assertRaisesRegex(ValueError, "Rust LLVM benchmark"):
                benchmark.run("unused", "unused", 1, 2, language, rust_async=asynchronous, rust_llvm=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def build(command, **_):
                bundle = Path(command[command.index("--output-dir") + 1])
                bundle.mkdir()
                (bundle / "build.json").write_text('{"schema_version":1,"backend":"compiler"}')

            with patch.object(benchmark.subprocess, "run", side_effect=build):
                with self.assertRaisesRegex(ValueError, "schema-2 llvm bundle"):
                    benchmark.rust_bundle(root, root, root / "policy", root / "app.rs", {}, "llvm")

    def test_rust_bundle_requires_the_requested_schema_and_backend(self):
        for backend, metadata in (("compiler", {"schema_version": 1}), ("compiler", {"schema_version": 3, "backend": "llvm"}), ("llvm", {"schema_version": 3, "backend": "compiler"})):
            with self.subTest(backend=backend, metadata=metadata), tempfile.TemporaryDirectory() as directory:
                root, output = self.fixture(directory)
                self.metadata = metadata
                with patch.object(benchmark.subprocess, "run", side_effect=self.build):
                    with self.assertRaisesRegex(ValueError, "Rust benchmark requires"):
                        benchmark.rust_bundle(root, output, root / "policy", root / "app.rs", {}, backend)

    def test_driver_records_real_application_identity_and_validates_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = self.fixture(directory)
            samples = {key: [{"elapsed_ns": ns, "checksum": 9, "calls": 10}] * 2 for key, ns in (("baseline", 10), ("metrics_off", 20), ("metrics_on", 30))}
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=self.launch), patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", return_value='{"pid":42}'), patch.object(benchmark, "measure", return_value=samples) as measure:
                report = benchmark.run(root, output, 10, 2, endpoint="http://127.0.0.1:1234")
                self.assertEqual(report["same_instrumented_pid"], 42)
                self.assertTrue(report["complete_telemetry"])
                self.assertEqual(measure.call_args.args[-1], 42)
                socket = measure.call_args.args[1]
                self.assertLessEqual(len(str(socket).encode()), 90)
                self.assertFalse(socket.parent.exists())
                self.telemetry["export_loss"] = 1
                with self.assertRaisesRegex(ValueError, "Incomplete telemetry"):
                    benchmark.run(root, output, 10, 2)
            with patch.object(benchmark.subprocess, "Popen", side_effect=self.launch), patch.object(benchmark, "read_line", return_value="wrong"), patch.object(benchmark.os, "killpg") as kill:
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
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=self.launch) as launch, patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", side_effect=['{"pid":42}', 'openjdk 21']), patch.object(benchmark, "measure", return_value=samples):
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
            with patch.object(benchmark.platform, "platform", return_value="test"), patch.object(benchmark.platform, "machine", return_value="arm64"), patch.object(benchmark.subprocess, "Popen", side_effect=self.launch) as launch, patch.object(benchmark, "read_line", return_value="ready"), patch.object(benchmark, "batch"), patch.object(benchmark.subprocess, "check_output", side_effect=['{"pid":42}', 'go version go1.27.1']), patch.object(benchmark, "measure", return_value=samples):
                report = benchmark.run(root, output, 10, 2, "go")
            self.assertEqual(report["language"], "go")
            self.assertEqual(launch.call_args_list[0].args[0][1], "run")
            self.assertIn("examples.apps.go_latency.process_order", (output / "policy.toml").read_text())
