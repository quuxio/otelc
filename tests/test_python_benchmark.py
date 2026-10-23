"""Real Python benchmark processes retain raw samples and reject unknown drains."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otelc import benchmark, benchmark_process, python_benchmark
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class PythonBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        data = copy.deepcopy(Policy.load(ROOT / 'examples/otelc.toml').data)
        data['benchmark'].update(iterations=10, samples=2, warmup=1)
        data['metrics'].update(collection=False, emission=False)
        data['traces']['sample_ratio'] = 1.0
        data['export'].update(endpoint=f'http://127.0.0.1:{self.capture.server.server_port}', interval_ms=100000)
        self.policy = Policy.parse(data)

    def test_native_five_modes_share_one_instrumented_pid_and_separate_sdk_costs(self) -> None:
        output = self.root / 'comparison'
        report = python_benchmark.run(self.policy, output, comparison_id='shared-example')
        self.assertEqual((report['language'], report['comparison_id'], report['expected_checksum']),
                         ('python', 'shared-example', 41284864))
        self.assertEqual(set(report['samples_ns_per_operation']), {'plain', *benchmark.STATES})
        self.assertTrue(all(len(values) == 2 for values in report['samples_ns_per_operation'].values()))
        self.assertEqual({row['pid'] for row in report['transitions']}, {report['pids']['instrumented']})
        self.assertEqual({row['sdk_pid'] for row in report['transitions']}, {report['sdk_pid']})
        self.assertNotEqual(report['pids']['instrumented'], report['sdk_pid'])
        raw = [json.loads(line) for line in (output / 'raw.jsonl').read_text().splitlines()]
        responses = [json.loads(line) for line in (output / 'responses.jsonl').read_text().splitlines()]
        self.assertEqual((len(raw), len(responses)), (10, 10))
        for row in raw:
            self.assertEqual(row['checksum'], report['expected_checksum'])
            self.assertEqual(set(row['process_usage']), {'application'} if row['mode'] == 'plain' else {'application', 'sdk'})
            for role, usage in row['process_usage'].items():
                self.assertGreaterEqual(usage['cpu_ns'], 0)
                self.assertGreater(usage['rss_bytes'], 0)
                expected = report['sdk_pid'] if role == 'sdk' else row['pid']
                self.assertEqual((usage['before']['pid'], usage['after']['pid']), (expected, expected))
        status = json.loads((output / 'instrumented/sdk/python-sdk-status.json').read_text())
        self.assertEqual(status['phase'], 'closed')
        self.assertEqual(status['producer']['function_completions'], 90)
        self.assertEqual(status['producer']['lifetimes']['resource_completed'], 90)
        self.assertEqual(self.capture.calls(), 40)
        self.assertEqual(json.loads((output / 'instrumented/exit.json').read_text()),
                         {'application_exit': 0, 'sdk_exit': 0})
        self.assertFalse((output / 'plain/sdk').exists())
        points = self.capture.points('otelc.benchmark.operation.duration')
        self.assertEqual(len(points), 5)
        self.assertTrue(all(point.count == 2 for point in points))
        self.assertTrue(self.capture.points('otelc.benchmark.process.cpu.duration'))
        self.assertTrue(self.capture.points('otelc.benchmark.process.rss'))
        for changes in ((('phase',), 'failed'), (('producer_checkpoint_available',), False),
                        (('protocol_errors',), 1), (('reply_lost',), None),
                        (('span_accounting', 'queue_pending'), 1),
                        (('producer', 'lifetimes', 'lost'), 1),
                        (('producer', 'runtime_losses', 'incomplete'), True)):
            path, replacement = changes
            changed = copy.deepcopy(status)
            target = changed
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = replacement
            with self.subTest(invalid=path), self.assertRaises(ValueError):
                python_benchmark.validate_drain(changed, report['pids']['instrumented'], report['sdk_pid'], lifetimes=True)

    def test_failed_final_validation_keeps_samples_without_driver_publication(self) -> None:
        output = self.root / 'failed'
        with mock.patch('otelc.python_benchmark.validate_drain', side_effect=ValueError('controlled failed drain')):
            with self.assertRaisesRegex(ValueError, 'controlled failed drain'):
                python_benchmark.run(self.policy, output)
        self.assertEqual(len((output / 'raw.jsonl').read_text().splitlines()), 10)
        self.assertFalse((output / 'report.json').exists())
        self.assertFalse(self.capture.points('otelc.benchmark.operation.duration'))
        self.assertEqual(json.loads((output / 'instrumented/exit.json').read_text())['sdk_exit'], 0)

    def test_valid_body_response_survives_a_later_flush_failure_without_fake_cpu_window(self) -> None:
        process = mock.Mock(pid=os.getpid())
        response = {'calls': 10, 'elapsed_ns': 1000, 'checksum': 42, 'pid': -1}
        usage = {'cpu_ns': 1, 'rss_bytes': 1024, 'pid': process.pid}
        with mock.patch('otelc.benchmark_process.process_usage', return_value=usage), \
                mock.patch('otelc.benchmark_process.benchmark.batch', return_value=response), \
                mock.patch('otelc.benchmark_process.control.request', side_effect=ValueError('flush failed')):
            with self.assertRaisesRegex(ValueError, 'flush failed'):
                benchmark_process.measured_batch(process, 10, self.root / 'control.json', 123,
                                                 self.root, 0, 'both_on')
        row = json.loads((self.root / 'responses.jsonl').read_text())
        self.assertEqual(row['pid'], process.pid)
        self.assertEqual(row['response'], response)
        self.assertFalse((self.root / 'raw.jsonl').exists())

    def test_disabled_policy_is_refused_before_any_process(self) -> None:
        output = self.root / 'refused'
        with mock.patch('otelc.python_benchmark.application') as launch, self.assertRaises(ValueError):
            python_benchmark.run(Policy.parse(self.policy.data | {'languages': ['java']}), output)
        launch.assert_not_called()
        self.assertFalse(output.exists())

    def test_failed_evidence_writes_cannot_leave_the_owned_sdk_running(self) -> None:
        started = []
        original_start = python_benchmark.SdkProcess.start
        original_write = Path.write_text

        def start(*args, **kwargs):
            sdk = original_start(*args, **kwargs)
            started.append(sdk)
            self.addCleanup(sdk.abort)
            return sdk

        def write(path, *args, **kwargs):
            if path.name in ('process.json', 'exit.json'):
                raise OSError('controlled evidence write failure')
            return original_write(path, *args, **kwargs)

        with mock.patch.object(python_benchmark.SdkProcess, 'start', side_effect=start), \
                mock.patch.object(Path, 'write_text', autospec=True, side_effect=write):
            with self.assertRaisesRegex(OSError, 'evidence write failure'):
                with python_benchmark.application(self.policy, self.root / 'runtime', plain=False):
                    self.fail('failed setup must not yield an application')
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].process.poll())

    def test_changed_source_identity_refuses_the_report_after_retaining_measurements(self) -> None:
        output = self.root / 'changed'
        actual = python_benchmark.file_identity(ROOT / 'examples/python/BenchmarkApp.py')
        with mock.patch('otelc.python_benchmark.file_identity',
                        side_effect=[actual, actual | {'sha256': '0' * 64}]):
            with self.assertRaisesRegex(ValueError, 'source changed'):
                python_benchmark.run(self.policy, output)
        self.assertEqual(len((output / 'raw.jsonl').read_text().splitlines()), 10)
        self.assertFalse((output / 'report.json').exists())

    def test_public_example_and_benchmark_cli_use_the_common_policy(self) -> None:
        policy = self.root / 'otelc.toml'
        original = (ROOT / 'examples/otelc.toml').read_text()
        original = original.replace('http://127.0.0.1:4320', f'http://127.0.0.1:{self.capture.server.server_port}')
        original = original.replace('iterations = 10000', 'iterations = 10').replace('samples = 15', 'samples = 1')
        original = original.replace('warmup = 5', 'warmup = 1')
        policy.write_text(original)
        for plain in (True, False):
            result = subprocess.run([sys.executable, '-I', '-S', '-B', str(ROOT / 'otelc/cli_bootstrap.py'), '--config', str(policy),
                'example', 'python', *(['--plain'] if plain else [])], cwd=ROOT,
                input='10\nquit\n', capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['checksum'], 41284864)
            self.assertEqual(bool(result.stderr), not plain)
            if not plain:
                self.assertTrue(result.stderr.startswith('Control descriptor: '))
        output = self.root / 'cli'
        result = subprocess.run([sys.executable, '-I', '-S', '-B', str(ROOT / 'otelc/cli_bootstrap.py'), '--config', str(policy),
            'benchmark', '--language', 'python', '--output', str(output), '--no-publish',
            '--comparison-id', 'cli-shared'], cwd=ROOT, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((output / 'report.json').read_text())
        self.assertEqual(report['comparison_id'], 'cli-shared')
        self.assertEqual(len(report['process_samples']), 5)
        self.assertEqual(json.loads(result.stdout)['run_id'], report['run_id'])
        self.assertFalse(self.capture.points('otelc.benchmark.operation.duration'))
