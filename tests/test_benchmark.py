"""Actual small paired benchmark with independent checksum and decoded export."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from otelc import benchmark
from otelc.config import Policy
from test_java_integration import Capture


class BenchmarkTests(unittest.TestCase):
    def test_excluded_sources_have_no_application_observations(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(Policy.load(Path("examples/otelc.toml")).data)
        data["sources"]["exclude"] = ["examples/**"]
        data["benchmark"].update(iterations=10, samples=1, warmup=1)
        data["export"]["endpoint"] = f"http://127.0.0.1:{capture.server.server_port}"
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            output = Path(directory) / "run"
            report = benchmark.run(Policy.parse(data), output)
            self.assertEqual(report["expected_checksum"], 41284864)
            self.assertEqual(capture.calls(), 0)
            self.assertFalse(capture.points("otelc.lifetime.admitted"))
            self.assertFalse(capture.traces)
            self.assertEqual(json.loads((output / "health.json").read_text())["lifetime_lost"], 0)

    def test_checksums_are_independent_and_deterministic(self) -> None:
        self.assertEqual(benchmark.checksum(0), 0)
        self.assertEqual(benchmark.checksum(10), 41284864)

    def test_plain_and_all_live_states_use_equivalent_work(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(Policy.load(Path("examples/otelc.toml")).data)
        data["benchmark"].update(iterations=10, samples=2, warmup=1)
        data["export"]["endpoint"] = f"http://127.0.0.1:{capture.server.server_port}"
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            output = Path(directory) / "run"
            report = benchmark.run(Policy.parse(data), output)
            self.assertEqual(set(report["samples_ns_per_operation"]), {"plain", *benchmark.STATES})
            self.assertTrue(all(len(values) == 2 for values in report["samples_ns_per_operation"].values()))
            self.assertEqual({item["pid"] for item in report["transitions"]}, {report["pids"]["instrumented"]})
            self.assertEqual(json.loads((output / "health.json").read_text())["lifetime_lost"], 0)
            self.assertEqual(len((output / "raw.jsonl").read_text().splitlines()), 10)
            self.assertTrue(capture.points("otelc.benchmark.operation.duration"))
            self.assertEqual(json.loads((output / "report.json").read_text())["expected_checksum"], 41284864)
