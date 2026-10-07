"""Check coverage orchestration and the 80% gate without compiling applications."""
import os
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
        with patch.object(rust_coverage, "coverage_environment", return_value={"MARKER": "value", "CARGO_TARGET_DIR": "/does-not-exist/otelc-test", "OTELC_COVERAGE_BIN_DIR": "/does-not-exist/otelc-test/maps"}):
            with patch.object(rust_coverage.subprocess, "run") as run:
                with patch.object(rust_coverage, "write_reports") as report:
                    rust_coverage.main()
        self.assertEqual(run.call_args_list[2].args[0][:2], ["cargo", "build"])
        self.assertEqual(run.call_args_list[3].args[0][:2], ["cargo", "test"])
        self.assertEqual(report.call_args.args[0]["MARKER"], "value")

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
            (target / "native-binaries").mkdir()
            (target / "native-binaries/app").write_text("map")
            (target / "test.profraw").write_bytes(b"profile")
            environment = {"CARGO_TARGET_DIR": directory, "OTELC_COVERAGE_BIN_DIR": str(target / "native-binaries")}
            for report, passes in (("DA:1,0\n", False), ("SF:/tmp/crates/rust-adapter/src/lib.rs\nDA:1,1\nSF:/tmp/crates/rust-probes/src/lib.rs\nDA:1,1\n", True)):
                with patch.object(rust_coverage.subprocess, "run"), patch.object(Path, "write_text"):
                    with patch.object(rust_coverage.subprocess, "check_output", return_value=report) as export:
                        if passes:
                            rust_coverage.write_reports(environment)
                        else:
                            with self.assertRaises(ValueError):
                                rust_coverage.write_reports(environment)
                self.assertTrue(any(str(target / "native-binaries/app") in call.args[0] for call in export.call_args_list))

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
        good = "SF:/tmp/crates/rust-adapter/src/lib.rs\nDA:1,1\nSF:/tmp/crates/rust-probes/src/lib.rs\nDA:1,1\n"
        rust_coverage.enforce_rust_adapter_coverage(good)
        with self.assertRaisesRegex(ValueError, "rust-probes"):
            rust_coverage.enforce_rust_adapter_coverage(good.replace("probes/src/lib.rs\nDA:1,1", "probes/src/lib.rs\nDA:1,0"))
        with self.assertRaisesRegex(ValueError, "no instrumented"):
            rust_coverage.enforce_rust_adapter_coverage("SF:/tmp/native.rs\nDA:1,1\n")
