"""Actual small paired benchmark with independent checksum and decoded export."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from otelc import benchmark, benchmark_process
from otelc.config import Policy
from test_java_integration import Capture


class BenchmarkTests(unittest.TestCase):
    def test_application_response_cannot_substitute_the_observed_process_identity(self) -> None:
        data = copy.deepcopy(Policy.load(Path('examples/otelc.toml')).data)
        data['benchmark'].update(iterations=1, samples=1, warmup=1)
        policy = Policy.parse(data)
        expected = benchmark.checksum(1)
        process = Mock(pid=123)
        response = {'calls': 1, 'checksum': expected, 'elapsed_ns': 10, 'pid': -1}
        usage = {'pid': process.pid, 'cpu_ns': 0, 'rss_bytes': 1024, 'observed_at_unix_ns': 1}
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            output = Path(directory)
            with patch.object(benchmark, 'batch', return_value=response), \
                    patch.object(benchmark_process, 'process_usage', return_value=usage):
                benchmark.measure_batches(process, policy, output, output / 'control.sock',
                                          plain=True, expected=expected, records=benchmark.Measurements())
            row = json.loads((output / 'raw.jsonl').read_text())
            self.assertEqual(row['pid'], process.pid)
            original = json.loads((output / 'responses.jsonl').read_text())
            self.assertEqual(original['pid'], process.pid)
            self.assertEqual(original['response']['pid'], -1)

    def test_excluded_sources_have_no_application_observations(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(Policy.load(Path("examples/otelc.toml")).data)
        data["sources"]["exclude"] = ["examples/**"]
        data["benchmark"].update(iterations=10, samples=1, warmup=1)
        data["export"]["endpoint"] = f"http://127.0.0.1:{capture.server.server_port}"
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            output = Path(directory) / "run"
            report = benchmark.run(Policy.parse(data), output, comparison_id='java-shared-comparison')
            self.assertEqual(report['comparison_id'], 'java-shared-comparison')
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
            self.assertEqual(report['schema_version'], 2)
            self.assertEqual(len(report['process_samples']), 10)
            self.assertIsNone(report['sdk_pid'])
            for sample in report['process_samples']:
                lane = 'plain' if sample['mode'] == 'plain' else 'instrumented'
                self.assertEqual(sample['pid'], report['pids'][lane])
                self.assertEqual(set(sample['process_usage']), {'application'})
                usage = sample['process_usage']['application']
                self.assertEqual(usage['before']['pid'], sample['pid'])
                self.assertEqual(usage['after']['pid'], sample['pid'])
                self.assertGreaterEqual(usage['cpu_ns'], 0)
                self.assertGreater(usage['rss_bytes'], 0)
                self.assertGreaterEqual(usage['after']['observed_at_unix_ns'], usage['before']['observed_at_unix_ns'])
            self.assertTrue(capture.points('otelc.benchmark.process.cpu.duration'))
            self.assertTrue(capture.points('otelc.benchmark.process.rss'))
            self.assertEqual(len((output / 'responses.jsonl').read_text().splitlines()), 10)

    def test_resource_failure_retains_actual_response_without_publishing_a_sample(self) -> None:
        data = copy.deepcopy(Policy.load(Path('examples/otelc.toml')).data)
        data['benchmark'].update(iterations=10, samples=1, warmup=1)
        observed = []

        def unavailable_after_batch(pid: int) -> dict[str, int]:
            observed.append(pid)
            if len(observed) == 2:
                raise ValueError('resource observation unavailable')
            return {'pid': pid, 'cpu_ns': 0, 'rss_bytes': 1024, 'observed_at_unix_ns': 1}

        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            output = Path(directory) / 'failed'
            with patch.object(benchmark_process, 'process_usage', side_effect=unavailable_after_batch), \
                    patch.object(benchmark, 'export_result') as publish:
                prepared_input = Policy.parse(data)
                with self.assertRaisesRegex(ValueError, 'resource observation unavailable'):
                    benchmark.run(prepared_input, output)
            publish.assert_not_called()
            self.assertFalse((output / 'report.json').exists())
            self.assertFalse((output / 'raw.jsonl').exists())
            rows = [json.loads(line) for line in (output / 'responses.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['response']['checksum'], 41284864)
            self.assertEqual(rows[0]['pid'], observed[0])
            self.assertEqual(observed, [observed[0], observed[0]])
