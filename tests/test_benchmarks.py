"""Ensure paired results reject changed outputs and expose incomplete telemetry."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import benchmark


class BenchmarkTests(unittest.TestCase):
    def test_workload_output_requires_valid_timing(self):
        self.assertEqual(benchmark.parse_output("elapsed_ns=2 checksum=0 calls=3"), {"elapsed_ns": 2, "checksum": 0, "calls": 3})
        for invalid in ("", "elapsed_ns=0 checksum=0 calls=1"):
            with self.assertRaises(ValueError):
                benchmark.parse_output(invalid)

    def test_invalid_workload_bounds(self):
        for arguments in ((1, 10, 1), (2, 0, 1), (2, 10, 17)):
            with self.assertRaises(ValueError):
                benchmark.benchmark(*arguments, Path("/unused"))

    def run_comparison(self, telemetry=None, changed_checksum=False):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "baseline").write_bytes(b"plain")
            (output / "instrumented").write_bytes(b"probes")
            def measure(command, environment):
                active = "OTELC_CONFIG" in environment
                if active:
                    report = {"drained": True, "export_finished": True, "function_calls": 1020, "losses": {"queue": 0}, "export_dropped_batches": 0}
                    report.update(telemetry or {})
                    Path(environment["OTELC_REPORT_PATH"]).write_text(json.dumps(report))
                return {"elapsed_ns": 200 if active else 100, "process_ns": 500, "checksum": 2 if active and changed_checksum else 1, "calls": 1020}
            read_text = Path.read_text
            def read(path, *args, **kwargs):
                if path.name == "otelc-llvm-toolchain.json":
                    return '{"bindir":"/matched/bin"}'
                return read_text(path, *args, **kwargs)
            with patch.object(Path, "read_text", autospec=True, side_effect=read), patch.object(benchmark.platform, "platform", return_value="Test host"), patch.object(benchmark, "measure", side_effect=measure), patch.object(benchmark.subprocess, "run"), patch.object(benchmark.subprocess, "check_output", return_value="Matched Clang\n"), contextlib.redirect_stdout(io.StringIO()):
                result = benchmark.benchmark(2, 20, 1, output)
            saved = json.loads((output / "results.json").read_text())
            self.assertEqual(saved, result)
            self.assertTrue((output / "results.md").is_file())
            return result

    def test_complete_comparison_retains_raw_samples(self):
        result = self.run_comparison()
        self.assertTrue(result["complete_telemetry"])
        self.assertTrue(result["checksum_verified"])
        self.assertEqual(result["summary"]["metrics_enabled"]["change_percent"], 100)
        self.assertEqual(len(result["samples"]["baseline"]), 2)

    def test_lost_or_uncounted_telemetry_is_disclosed(self):
        for incomplete in ({"losses": {"queue": 1}}, {"function_calls": 1019}, {"drained": False}, {"export_finished": False}, {"export_dropped_batches": 1}):
            self.assertFalse(self.run_comparison(incomplete)["complete_telemetry"])

    def test_changed_application_output_rejects_results(self):
        with self.assertRaisesRegex(ValueError, "checksums differ"):
            self.run_comparison(changed_checksum=True)

    def test_measure_records_whole_process_time(self):
        class Result:
            stdout = "elapsed_ns=12 checksum=3 calls=5"
        with patch.object(benchmark.time, "perf_counter_ns", side_effect=[10, 110]), patch.object(benchmark.subprocess, "run", return_value=Result()):
            self.assertEqual(benchmark.measure(["app"], {})["process_ns"], 100)
