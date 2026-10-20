"""Check coverage orchestration and the 80% gate without compiling applications."""
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from ci import rust_coverage


class CoverageTests(unittest.TestCase):
    def test_exports_are_parsed_without_shell_execution(self):
        settings = "export LLVM_PROFILE_FILE='/tmp/profiles with spaces/%p.profraw'\nignored\nexport VALUE='$(never execute)'\n"
        with patch.object(rust_coverage, "llvm_environment"):
            with patch.object(rust_coverage.subprocess, "check_output", return_value=settings):
                environment = rust_coverage.coverage_environment()
        self.assertEqual(environment["LLVM_PROFILE_FILE"], "/tmp/profiles with spaces/%p.profraw")
        self.assertEqual(environment["VALUE"], "$(never execute)")
        self.assertIn("coverage-build", environment["CARGO_TARGET_DIR"])
        self.assertNotIn("RUSTC_WRAPPER", environment)

    def test_line_coverage_and_empty_report(self):
        self.assertEqual(rust_coverage.line_coverage("DA:1,2\nDA:2,0\n"), 50)
        with self.assertRaises(ValueError):
            rust_coverage.line_coverage("")

    def test_duplicate_native_maps_merge_hits_and_canonical_paths(self):
        report = rust_coverage.merge_line_reports(["SF:/tmp/code/../app.rs\nDA:1,0\nDA:2,0\n", "SF:/tmp/app.rs\nDA:1,3\nDA:2,0\n"])
        self.assertEqual(report.count("SF:"), 1)
        self.assertIn("DA:1,3", report)
        self.assertEqual(rust_coverage.line_coverage(report), 50)

    def test_build_precedes_native_tests_and_reports(self):
        with patch("ci.rust_mir_coverage.run", return_value="MIR report"), patch("ci.rust_llvm_coverage.run", return_value="LLVM 23 report"), patch.object(rust_coverage, "coverage_environment", return_value={"MARKER": "value", "CARGO_TARGET_DIR": "/does-not-exist/otelc-test", "OTELC_COVERAGE_BIN_DIR": "/does-not-exist/otelc-test/maps"}):
            with patch.object(rust_coverage.subprocess, "run") as run:
                with patch.object(rust_coverage, "write_reports") as report:
                    rust_coverage.main()
        self.assertEqual(run.call_args_list[3].args[0][:2], ["cargo", "build"])
        self.assertEqual(run.call_args_list[4].args[0][:2], ["cargo", "test"])
        self.assertEqual(run.call_args_list[5].args[0][:2], ["node", "--test"])
        self.assertEqual(report.call_args.args[0]["MARKER"], "value")
        self.assertEqual(report.call_args.args[1], ["LLVM 23 report", "MIR report"])

    def test_combined_coverage_rejects_source_changes_between_toolchains(self):
        with patch("ci.rust_mir_coverage.run", return_value="MIR report"), patch("ci.rust_llvm_coverage.run", return_value="report"), patch("ci.rust_mir_coverage.source_hashes", side_effect=[{"input": "before"}, {"input": "after"}]):
            with patch.object(rust_coverage, "coverage_environment", return_value={"CARGO_TARGET_DIR": "/does-not-exist/otelc-test", "OTELC_COVERAGE_BIN_DIR": "/does-not-exist/otelc-test/maps"}), patch.object(rust_coverage.subprocess, "run"), patch.object(rust_coverage, "write_reports") as report:
                with self.assertRaisesRegex(ValueError, "sources changed"):
                    rust_coverage.main()
        report.assert_not_called()

    def test_different_llvm_versions_combine_only_exported_source_lines(self):
        stable = "SF:/tmp/adapter.rs\nDA:1,1\nDA:2,0\n"
        compiler = "SF:/tmp/adapter.rs\nDA:1,0\nDA:2,3\n"
        merged = rust_coverage.merge_line_reports([stable, compiler])
        self.assertEqual(rust_coverage.line_coverage(merged), 100)
        self.assertEqual(merged.count("SF:"), 1)

    def test_failure_stops_reports(self):
        with patch.object(rust_coverage, "coverage_environment", return_value={"CARGO_TARGET_DIR": "/does-not-exist/otelc-test", "OTELC_COVERAGE_BIN_DIR": "/does-not-exist/otelc-test/maps"}):
            with patch.object(rust_coverage.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "cargo")) as run:
                with patch.object(rust_coverage, "write_reports") as report:
                    with self.assertRaises(subprocess.CalledProcessError):
                        rust_coverage.main()
        self.assertEqual(run.call_count, 1)
        report.assert_not_called()

    def test_coverage_gate_and_native_maps(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "debug/deps").mkdir(parents=True)
            sdk_map = target / "debug/deps/rust-sdk-test"
            sdk_map.write_text("SDK test coverage map")
            sdk_map.chmod(0o700)
            (target / "native-binaries").mkdir()
            (target / "native-binaries/app").write_text("map")
            (target / "test.profraw").write_bytes(b"profile")
            environment = {"CARGO_TARGET_DIR": directory, "OTELC_COVERAGE_BIN_DIR": str(target / "native-binaries")}
            for report, passes in (("DA:1,0\n", False), ("SF:/tmp/crates/rust-adapter/src/lib.rs\nDA:1,1\nSF:/tmp/crates/rust-probes/src/lib.rs\nDA:1,1\nSF:/tmp/crates/process/src/lib.rs\nDA:1,1\nSF:/tmp/native/node/PromiseObserver.cpp\nDA:1,1\nSF:/tmp/crates/rust-probes/src/traces.rs\nDA:1,1\nSF:/tmp/crates/export/src/traces.rs\nDA:1,1\nSF:/tmp/crates/runtime/src/traces.rs\nDA:1,1\nSF:/tmp/crates/rust-adapter/src/artifact.rs\nDA:1,1\nSF:/tmp/crates/rust-probes/src/compiler_abi.rs\nDA:1,1\nSF:/tmp/crates/rust-adapter/src/compiler_inventory.rs\nDA:1,1\n", True)):
                if passes:
                    report += "SF:/tmp/crates/rust-probes/src/compiler_mir.rs\nDA:1,1\nSF:/tmp/crates/rust-probes/src/recording.rs\nDA:1,1\n"
                with patch.object(rust_coverage.subprocess, "run"), patch.object(Path, "write_text"):
                    def export_map(command, **_):
                        # LLVM can select a zero-hit library map when exported C
                        # symbols collide across objects. Only the isolated SDK
                        # test map carries the matching compiler-hook profile.
                        objects = [command[index + 1] for index, value in enumerate(command) if value == "--object"]
                        if objects == [str(sdk_map)]:
                            return report
                        return report.replace("/compiler_abi.rs\nDA:1,1", "/compiler_abi.rs\nDA:1,0")

                    with patch.object(rust_coverage.subprocess, "check_output", side_effect=export_map) as export:
                        if passes:
                            rust_coverage.write_reports(environment)
                        else:
                            with self.assertRaises(ValueError):
                                rust_coverage.write_reports(environment)
                self.assertTrue(any(str(target / "native-binaries/app") in call.args[0] for call in export.call_args_list))
                for call in export.call_args_list:
                    ignored = next(argument.split("=", 1)[1] for argument in call.args[0] if argument.startswith("--ignore-filename-regex="))
                    self.assertIsNotNone(re.search(ignored, "/Users/runner/hostedtoolcache/node/24/include/node/v8-local-handle.h"))
                    self.assertIsNone(re.search(ignored, "/Users/runner/work/otelc/otelc/native/node/PromiseObserver.cpp"))

    def test_preconfigured_llvm_tools(self):
        environment = {"LLVM_COV": "cov", "LLVM_PROFDATA": "prof"}
        with patch.object(rust_coverage.subprocess, "check_output") as run:
            rust_coverage.llvm_environment(environment)
        run.assert_not_called()

    def test_find_matching_tools(self):
        with patch.object(rust_coverage.subprocess, "check_output", side_effect=["host: native\nLLVM version: 22.1.8\n", "/sysroot\n"]):
            with patch.object(Path, "is_file", return_value=True):
                environment = {}
                rust_coverage.llvm_environment(environment)
        self.assertEqual(environment["LLVM_COV"], "/opt/homebrew/opt/llvm@22/bin/llvm-cov")

    def test_missing_tools_fail(self):
        with patch.object(rust_coverage.subprocess, "check_output", side_effect=["host: native\nLLVM version: 22.1.8\n", "/sysroot\n"]):
            with patch.object(Path, "is_file", return_value=False):
                with self.assertRaises(ValueError):
                    rust_coverage.llvm_environment({})

    def test_missing_profiles_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                rust_coverage.write_reports({"CARGO_TARGET_DIR": directory})

    def test_language_coverage_cannot_be_hidden_in_native_aggregate(self):
        good = "SF:/tmp/crates/rust-adapter/src/lib.rs\nDA:1,1\nSF:/tmp/crates/rust-probes/src/lib.rs\nDA:1,1\nSF:/tmp/crates/process/src/lib.rs\nDA:1,1\n"
        rust_coverage.enforce_rust_adapter_coverage(good)
        missing_probes = good.replace("probes/src/lib.rs\nDA:1,1", "probes/src/lib.rs\nDA:1,0")
        with self.assertRaisesRegex(ValueError, "rust-probes"):
            rust_coverage.enforce_rust_adapter_coverage(missing_probes)
        missing_process = good.replace("process/src/lib.rs\nDA:1,1", "process/src/lib.rs\nDA:1,0")
        with self.assertRaisesRegex(ValueError, "process"):
            rust_coverage.enforce_rust_adapter_coverage(missing_process)
        with self.assertRaisesRegex(ValueError, "no instrumented"):
            rust_coverage.enforce_rust_adapter_coverage("SF:/tmp/native.rs\nDA:1,1\n")
        rust_coverage.enforce_node_observer_coverage("SF:/tmp/native/node/PromiseObserver.cpp\nDA:1,1\n")
        with self.assertRaisesRegex(ValueError, "Node native"):
            rust_coverage.enforce_node_observer_coverage("SF:/tmp/native/node/PromiseObserver.cpp\nDA:1,0\n")
        with self.assertRaisesRegex(ValueError, "no instrumented"):
            rust_coverage.enforce_node_observer_coverage(good)

    def test_trace_and_build_artifact_files_have_their_own_coverage_floors(self):
        good = "SF:/tmp/crates/rust-probes/src/traces.rs\nDA:1,1\nSF:/tmp/crates/export/src/traces.rs\nDA:1,1\nSF:/tmp/crates/runtime/src/traces.rs\nDA:1,1\nSF:/tmp/crates/rust-adapter/src/artifact.rs\nDA:1,1\nSF:/tmp/crates/rust-probes/src/compiler_abi.rs\nDA:1,1\nSF:/tmp/crates/rust-adapter/src/compiler_inventory.rs\nDA:1,1\n"
        good += "SF:/tmp/crates/rust-probes/src/compiler_mir.rs\nDA:1,1\nSF:/tmp/crates/rust-probes/src/recording.rs\nDA:1,1\n"
        rust_coverage.enforce_rust_file_coverage(good)
        no_hits = good.replace("DA:1,1", "DA:1,0")
        with self.assertRaisesRegex(ValueError, "trace context"):
            rust_coverage.enforce_rust_file_coverage(no_hits)
        missing_store = good.replace("/crates/export/src/traces.rs\nDA:1,1", "/crates/export/src/traces.rs\nDA:1,0")
        with self.assertRaisesRegex(ValueError, "trace store"):
            rust_coverage.enforce_rust_file_coverage(missing_store)
        missing_bridge = good.replace("/crates/runtime/src/traces.rs\nDA:1,1", "/crates/runtime/src/traces.rs\nDA:1,0")
        with self.assertRaisesRegex(ValueError, "Native trace bridge"):
            rust_coverage.enforce_rust_file_coverage(missing_bridge)
        missing_artifacts = good.replace("/crates/rust-adapter/src/artifact.rs\nDA:1,1", "/crates/rust-adapter/src/artifact.rs\nDA:1,0")
        with self.assertRaisesRegex(ValueError, "Rust build artifacts"):
            rust_coverage.enforce_rust_file_coverage(missing_artifacts)
        for path, label in (("/crates/rust-probes/src/compiler_abi.rs", "Rust compiler ABI"),
                            ("/crates/rust-adapter/src/compiler_inventory.rs", "Rust compiler inventory"),
                            ("/crates/rust-probes/src/compiler_mir.rs", "Rust compiler future observers"),
                            ("/crates/rust-probes/src/recording.rs", "Rust SDK recording suppression")):
            missing_component = good.replace(path + "\nDA:1,1", path + "\nDA:1,0")
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, label):
                rust_coverage.enforce_rust_file_coverage(missing_component)
        with self.assertRaisesRegex(ValueError, "no instrumented"):
            rust_coverage.enforce_rust_file_coverage("SF:/tmp/crates/rust-probes/src/lib.rs\nDA:1,1\n")
