"""Final collection-only samples must reach the receiver before SDK shutdown."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from otelc import benchmark_process, control, node_benchmark, python_benchmark
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkFinalizationTests(unittest.TestCase):
    def test_changed_final_process_or_gate_retains_response_and_refuses(self) -> None:
        valid = {'pid': 101, 'sdk_pid': 202, 'collection': True, 'emission': True}
        for stage in ('restored', 'flushed'):
            for key, value in (('pid', 303), ('sdk_pid', 404), ('collection', False), ('emission', False)):
                with self.subTest(stage=stage, key=key), tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary)
                    wrong = valid | {key: value}
                    responses = [wrong] if stage == 'restored' else [valid, wrong]
                    with patch('otelc.benchmark_process.control.request', side_effect=responses) as request:
                        with self.assertRaisesRegex(ValueError, 'final metric state or SDK process changed'):
                            benchmark_process.finalize_metrics(Mock(pid=101), output / 'control.json', 202, output)
                    receipt = json.loads((output / 'metric-finalization.json').read_text())
                    self.assertEqual(receipt[stage], wrong)
                    self.assertEqual(request.call_count, len(responses))

    def test_failed_final_flush_keeps_samples_and_prevents_publication(self) -> None:
        for language in ('javascript', 'python'):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary:
                capture = Capture()
                try:
                    data = copy.deepcopy(Policy.load(ROOT / 'examples/otelc.toml').data)
                    data['benchmark'].update(iterations=10, samples=4, warmup=1)
                    data['metrics'].update(collection=False, emission=False)
                    data['export'].update(endpoint=f'http://127.0.0.1:{capture.server.server_port}', interval_ms=100000)
                    policy = Policy.parse(data)
                    output = Path(temporary).resolve() / 'comparison'
                    restored = False
                    original = control.request

                    def request(endpoint: Path, command: str) -> dict:
                        nonlocal restored
                        if command == 'on':
                            restored = True
                        if command == 'flush' and restored:
                            raise RuntimeError('final flush failed')
                        return original(endpoint, command)

                    with patch('otelc.benchmark_process.control.request', side_effect=request), \
                            patch('otelc.benchmark.export_result') as publish:
                        with self.assertRaisesRegex(RuntimeError, 'final flush failed'):
                            if language == 'python':
                                python_benchmark.run(policy, output)
                            else:
                                node_benchmark.run(policy, output, language=language)
                        publish.assert_not_called()
                    self.assertFalse((output / 'report.json').exists())
                    self.assertEqual(len((output / 'raw.jsonl').read_text().splitlines()), 20)
                    self.assertEqual(set(json.loads((output / 'metric-finalization.json').read_text())),
                                     {'schema_version', 'restored'})
                finally:
                    capture.close()

    def test_all_collected_calls_survive_the_final_emission_only_state(self) -> None:
        for language, backend in (('javascript', 'native'), ('typescript', 'native'),
                                  ('typescript', 'api'), ('python', 'native')):
            with self.subTest(language=language, backend=backend), \
                    tempfile.TemporaryDirectory(prefix='metric-finalization-') as temporary:
                capture = Capture()
                try:
                    data = copy.deepcopy(Policy.load(ROOT / 'examples/otelc.toml').data)
                    data['benchmark'].update(iterations=10, samples=4, warmup=1)
                    data['metrics'].update(collection=False, emission=False)
                    data['export'].update(endpoint=f'http://127.0.0.1:{capture.server.server_port}', interval_ms=100000)
                    policy = Policy.parse(data)
                    output = Path(temporary).resolve() / 'comparison'
                    if language == 'python':
                        report = python_benchmark.run(policy, output, publish=False)
                    else:
                        report = node_benchmark.run(policy, output, language=language, backend=backend, publish=False)
                    self.assertEqual(len(report['samples_ns_per_operation']['both_on']), 4)
                    self.assertEqual(report['transitions'][-1]['mode'], 'emission_only')
                    self.assertEqual(capture.calls(), 80)
                    final = json.loads((output / 'metric-finalization.json').read_text())
                    for key in ('restored', 'flushed'):
                        self.assertEqual(final[key]['pid'], report['pids']['instrumented'])
                        self.assertEqual(final[key]['sdk_pid'], report['sdk_pid'])
                        self.assertIs(final[key]['collection'], True)
                        self.assertIs(final[key]['emission'], True)
                    raw = [json.loads(row) for row in (output / 'raw.jsonl').read_text().splitlines()]
                    self.assertEqual(len(raw), 20)
                finally:
                    capture.close()
