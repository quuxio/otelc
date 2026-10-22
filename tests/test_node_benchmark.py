"""Real source-free Node comparisons retain both process roles and clean drain evidence."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from otelc import benchmark, node_benchmark
from otelc.config import Policy
from test_java_integration import Capture


class NodeBenchmarkTests(unittest.TestCase):
    def test_equivalent_languages_and_compilers_keep_one_live_instrumented_pid(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(Policy.load(Path('examples/otelc.toml')).data)
        data['benchmark'].update(iterations=10, samples=2, warmup=1)
        data['metrics'].update(collection=False, emission=False)
        data['export']['endpoint'] = f'http://127.0.0.1:{capture.server.server_port}'
        policy = Policy.parse(data)
        for language, backend in (('javascript', 'native'), ('typescript', 'native'), ('typescript', 'api')):
            with self.subTest(language=language, backend=backend), tempfile.TemporaryDirectory(dir='/tmp') as temporary:
                output = Path(temporary) / 'comparison'
                report = node_benchmark.run(policy, output, language=language, backend=backend,
                                            comparison_id='shared-comparison')
                self.assertEqual(report['comparison_id'], 'shared-comparison')
                self.assertEqual(report['expected_checksum'], 41284864)
                self.assertEqual(set(report['samples_ns_per_operation']), {'plain', *benchmark.STATES})
                self.assertTrue(all(len(values) == 2 for values in report['samples_ns_per_operation'].values()))
                self.assertEqual({item['pid'] for item in report['transitions']}, {report['pids']['instrumented']})
                self.assertEqual({item['sdk_pid'] for item in report['transitions']}, {report['sdk_pid']})
                self.assertNotEqual(report['sdk_pid'], report['pids']['instrumented'])
                raw = [json.loads(line) for line in (output / 'raw.jsonl').read_text().splitlines()]
                self.assertEqual(len(raw), 10)
                for item in raw:
                    self.assertEqual(item['checksum'], report['expected_checksum'])
                    self.assertEqual(set(item['process_usage']), {'application'} if item['mode'] == 'plain' else {'application', 'sdk'})
                    for usage in item['process_usage'].values():
                        self.assertGreaterEqual(usage['cpu_ns'], 0)
                        self.assertGreater(usage['rss_bytes'], 0)
                final = json.loads((output / 'instrumented/node-status.json').read_text())
                self.assertEqual(final['phase'], 'closed')
                self.assertEqual(final['span_accounting']['queue_dropped'], 0)
                self.assertEqual(json.loads((output / 'instrumented/node-inputs.json').read_text())['phase'], 'verified')
                self.assertEqual(json.loads((output / 'plain/node-plain-inputs.json').read_text())['phase'], 'verified')
                self.assertTrue(capture.points('otelc.benchmark.process.cpu.duration'))
                self.assertTrue(capture.points('otelc.benchmark.process.rss'))
                durations = [point for point in capture.points('otelc.benchmark.operation.duration')
                             if any(item.key == 'run_id' and item.value.string_value == report['run_id'] for item in point.attributes)]
                self.assertEqual(len(durations), 5)
                for point in durations:
                    attributes = {item.key: item.value.string_value for item in point.attributes}
                    self.assertEqual(attributes['comparison_id'], 'shared-comparison')
                    self.assertEqual(attributes['language'], language)
                    self.assertEqual(attributes['compiler'], report['compiler'])
                    self.assertEqual(point.count, 2)
                self.assertEqual(json.loads((output / 'report.json').read_text())['run_id'], report['run_id'])
                for path, value in ((('span_accounting', 'queue_dropped'), 1),
                                    (('span_accounting', 'queue_pending'), None),
                                    (('handoff', 'lost'), 1),
                                    (('handoff_incomplete',), 1),
                                    (('export_failed',), 1),
                                    (('producer_checkpoint_available',), False)):
                    changed = copy.deepcopy(final)
                    target = changed
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    with self.subTest(invalid=path), self.assertRaises(ValueError):
                        node_benchmark.validate_drain(changed, report['pids']['instrumented'], report['sdk_pid'])

    def test_cpu_time_parser_retains_ps_precision(self) -> None:
        self.assertEqual(node_benchmark.cpu_time_ns('0:00.25'), 250_000_000)
        self.assertEqual(node_benchmark.cpu_time_ns('120:00.00'), 7_200_000_000_000)
        self.assertEqual(node_benchmark.cpu_time_ns('01:02:03'), 3_723_000_000_000)
        self.assertEqual(node_benchmark.cpu_time_ns('2-01:02:03'), 176_523_000_000_000)
        for value in ('bad', '-1:00', '00:99', '00:NaN'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                node_benchmark.cpu_time_ns(value)

    def test_incomplete_drain_retains_raw_results_without_publishing_driver_metrics(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(Policy.load(Path('examples/otelc.toml')).data)
        data['benchmark'].update(iterations=10, samples=1, warmup=1)
        data['export']['endpoint'] = f'http://127.0.0.1:{capture.server.server_port}'
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            output = Path(temporary) / 'comparison'
            with patch('otelc.node_benchmark.validate_drain', side_effect=ValueError('incomplete drain')):
                with self.assertRaisesRegex(ValueError, 'incomplete drain'):
                    node_benchmark.run(Policy.parse(data), output, language='javascript')
            self.assertEqual(len((output / 'raw.jsonl').read_text().splitlines()), 5)
            self.assertFalse((output / 'report.json').exists())
            self.assertEqual(json.loads((output / 'instrumented/node-status.json').read_text())['phase'], 'closed')
            self.assertEqual(json.loads((output / 'instrumented/node-inputs.json').read_text())['phase'], 'verified')
            self.assertFalse(capture.points('otelc.benchmark.operation.duration'))
