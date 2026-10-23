"""Real Python benchmark processes retain raw samples and reject unknown drains."""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otelc import benchmark, benchmark_process, python_benchmark
from otelc import python_benchmark_launch as launch
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
        self.assertEqual(report['launch_profile'], 'prepared-native-v1')
        for lane in ('plain', 'instrumented'):
            receipt = json.loads((output / lane / 'python-execution.json').read_text())
            self.assertEqual(receipt['phase'], 'closed')
            self.assertEqual(receipt['application']['pid'], report['pids'][lane])
            self.assertEqual(receipt['supervisor_pid'], report['supervisor_pids'][lane])
            self.assertNotEqual(receipt['supervisor_pid'], receipt['application']['pid'])
            self.assertTrue(receipt['input_recheck'])
            self.assertEqual(receipt['entry_evidence']['phase'], 'complete')
            self.assertFalse(receipt['source_complete'])
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
            self.assertEqual(set(row['process_usage']), {'application', 'supervisor'} if row['mode'] == 'plain' else {'application', 'supervisor', 'sdk'})
            for role, usage in row['process_usage'].items():
                self.assertGreaterEqual(usage['cpu_ns'], 0)
                self.assertGreater(usage['rss_bytes'], 0)
                expected = (report['sdk_pid'] if role == 'sdk' else
                            report['supervisor_pids']['plain' if row['mode'] == 'plain' else 'instrumented']
                            if role == 'supervisor' else row['pid'])
                self.assertEqual((usage['before']['pid'], usage['after']['pid']), (expected, expected))
        status = json.loads((output / 'instrumented/sdk/python-sdk-status.json').read_text())
        self.assertEqual(status['phase'], 'closed')
        self.assertEqual(status['producer']['function_completions'], 90)
        self.assertEqual(status['producer']['lifetimes']['resource_completed'], 90)
        self.assertEqual(self.capture.calls(), 40)
        execution = json.loads((output / 'instrumented/python-execution.json').read_text())
        self.assertEqual((execution['application']['returncode'], execution['sdk']['returncode']), (0, 0))
        for lane, evidence in report['launch_evidence'].items():
            self.assertEqual(len(evidence), 6 if lane == 'plain' else 7)
            for name, recorded in evidence.items():
                self.assertEqual(launch.identity(output / lane / name), recorded)
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
        self.assertEqual(json.loads((output / 'instrumented/python-execution.json').read_text())['sdk']['returncode'], 0)

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
        original_start = launch.start
        original_write = launch.files.write

        def start(*args, **kwargs):
            process = original_start(*args, **kwargs)
            started.append(process)
            return process

        def write(path, *args, **kwargs):
            if path.name == 'process.json':
                raise OSError('controlled evidence write failure')
            return original_write(path, *args, **kwargs)

        with mock.patch.object(launch, 'start', side_effect=start), \
                mock.patch.object(launch.files, 'write', side_effect=write):
            with self.assertRaisesRegex(OSError, 'evidence write failure'):
                with python_benchmark.application(self.policy, self.root / 'runtime', plain=False):
                    self.fail('failed setup must not yield an application')
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].owner.poll())
        for pid in (started[0].pid, started[0].sdk_pid):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

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

    def test_changed_completed_lane_evidence_blocks_publication_after_retaining_all_samples(self) -> None:
        output = self.root / 'changed-evidence'
        original = python_benchmark.measure_process

        def measure(policy, directory, lane, *args):
            if lane == 'instrumented':
                path = output / 'plain/python-execution.json'
                changed = json.loads(path.read_text())
                changed['phase'] = 'failed'
                path.write_text(json.dumps(changed))
            return original(policy, directory, lane, *args)

        with mock.patch.object(python_benchmark, 'measure_process', side_effect=measure):
            with self.assertRaisesRegex(ValueError, 'retained evidence changed'):
                python_benchmark.run(self.policy, output)
        self.assertEqual(len((output / 'raw.jsonl').read_text().splitlines()), 10)
        self.assertFalse((output / 'report.json').exists())
        self.assertFalse(self.capture.points('otelc.benchmark.operation.duration'))

    def test_final_native_journal_and_typed_owner_receipts_are_checked_independently(self) -> None:
        runtime = self.root / 'checked'
        with launch.application(self.policy, runtime, plain=False) as process:
            self.assertEqual(benchmark.batch(process, 10)['checksum'], 41284864)
        execution_path = runtime / 'python-execution.json'
        original = execution_path.read_bytes()
        value = json.loads(original)
        changes = [(('schema_version',), True), (('phase',), 'failed'),
            (('source_complete',), True), (('input_recheck',), False),
            (('application', 'pid'), process.supervisor_pid),
            (('application', 'returncode'), False), (('sdk', 'returncode'), False),
            (('application', 'pid'), float(process.pid)),
            (('sdk', 'pid'), float(process.sdk_pid)),
            (('supervisor_pid',), float(process.supervisor_pid)),
            (('sdk', 'launch_id'), '0' * 32), (('bootstrap_recheck',), None),
            (('entry_evidence', 'capture_failures'), False)]
        for path, replacement in changes:
            changed = copy.deepcopy(value)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            execution_path.write_text(json.dumps(changed))
            try:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    launch.completed(process, runtime, self.policy, plain=False)
            finally:
                execution_path.write_bytes(original)
        sdk_path = runtime / 'sdk/python-sdk-status.json'
        sdk_bytes = sdk_path.read_bytes()
        for key, replacement in (('schema_version', True), ('kind', 'foreign'),
                                 ('pid', process.sdk_pid), ('pid', float(process.pid)), ('policy_sha256', '0' * 64)):
            changed = json.loads(sdk_bytes)
            changed[key] = replacement
            sdk_path.write_text(json.dumps(changed))
            try:
                with self.subTest(sdk=key), self.assertRaises(ValueError):
                    launch.completed(process, runtime, self.policy, plain=False)
            finally:
                sdk_path.write_bytes(sdk_bytes)
        journal_path = runtime / 'python-inputs.journal'
        journal = journal_path.read_bytes()
        journal_path.write_bytes(journal[:-1])
        with self.assertRaises(ValueError):
            launch.completed(process, runtime, self.policy, plain=False)
        journal_path.write_bytes(journal)
        launch.completed(process, runtime, self.policy, plain=False)

    def test_startup_requires_real_owner_bound_typed_identities_and_bounded_readiness(self) -> None:
        runtime = self.root / 'startup'
        launch.prepared.command(self.policy, runtime, root=ROOT,
                                entry=ROOT / 'examples/python/BenchmarkApp.py')
        owner = mock.Mock(pid=1234)
        owner.poll.return_value = 0
        with self.assertRaisesRegex(RuntimeError, 'before application readiness'):
            launch.start(owner, runtime, plain=False)
        owner.poll.return_value = None
        with self.assertRaises(TimeoutError):
            launch.start(owner, runtime, plain=False, timeout=0)
        value = {**launch.bindings(runtime, owner), 'schema_version': 1,
            'kind': 'otelc.python.application', 'application_pid': 2345,
            'sdk_pid': 3456, 'sdk_launch_id': '1' * 32, 'sdk_ready': True, 'native_command': []}
        path = runtime / 'python-application-start.json'
        launch.files.write(path, value)
        for key, replacement in (('schema_version', True), ('kind', 'foreign'),
                ('supervisor_pid', 4567), ('supervisor_pid', 1234.0), ('sdk_ready', False), ('application_pid', True),
                ('application_pid', 1234), ('sdk_pid', None), ('sdk_launch_id', None)):
            changed = {**value, key: replacement}
            path.write_text(json.dumps(changed))
            with self.subTest(field=key), self.assertRaises(ValueError):
                launch.start(owner, runtime, plain=False)
        path.write_text(json.dumps(value))
        with mock.patch.object(launch.os, 'getpgid', return_value=9999):
            with self.assertRaisesRegex(ValueError, 'outside its owned group'):
                launch.start(owner, runtime, plain=False)
        owner.poll.return_value = 0
        with self.assertRaisesRegex(RuntimeError, 'owner exited'):
            launch.start(owner, runtime, plain=False)

    def test_primary_cancellation_and_body_errors_survive_secondary_cleanup_failure(self) -> None:
        for index, primary in enumerate((KeyboardInterrupt('cancelled'), SystemExit(7), RuntimeError('body failed'))):
            runtime = self.root / ('cancelled-' + str(index))
            with self.subTest(kind=type(primary).__name__), \
                    mock.patch.object(launch.prepared, 'stop_group', side_effect=OSError('cleanup failed')):
                with self.assertRaises(type(primary)) as raised:
                    with launch.application(self.policy, runtime, plain=True) as process:
                        raise primary
            self.assertIs(raised.exception, primary)
            self.assertIn('cleanup also failed: OSError', primary.__notes__[-1])
            self.assertIsNotNone(process.owner.poll())

    def test_cleanup_failure_without_a_primary_error_is_not_reported_as_success(self) -> None:
        runtime = self.root / 'cleanup-failed'
        with mock.patch.object(launch.prepared, 'stop_group', side_effect=OSError('cleanup failed')):
            with self.assertRaisesRegex(OSError, 'cleanup failed'):
                with launch.application(self.policy, runtime, plain=True) as process:
                    pass  # No workload is needed to test final ownership cleanup.
        self.assertIsNotNone(process.owner.poll())
        self.assertEqual(process.evidence, {})

    def test_actual_sdk_death_prevents_publication_and_reaps_the_owned_pair(self) -> None:
        output = self.root / 'dead-sdk'
        original = benchmark_process.measure_batches
        owned = []

        def measure(process, policy, directory, endpoint, sdk_pid, *args, **kwargs):
            if endpoint is not None:
                owned.append(process)
                os.kill(sdk_pid, signal.SIGKILL)
            return original(process, policy, directory, endpoint, sdk_pid, *args, **kwargs)

        with mock.patch.object(benchmark_process, 'measure_batches', side_effect=measure):
            with self.assertRaises((ValueError, RuntimeError, OSError, subprocess.SubprocessError)):
                python_benchmark.run(self.policy, output)
        self.assertEqual(len(owned), 1)
        self.assertIsNotNone(owned[0].owner.poll())
        for pid in (owned[0].pid, owned[0].sdk_pid):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assertFalse((output / 'report.json').exists())
        self.assertFalse(self.capture.points('otelc.benchmark.operation.duration'))
        self.assertEqual(len((output / 'raw.jsonl').read_text().splitlines()), 2)
        final = json.loads((output / 'instrumented/python-execution.json').read_text())
        self.assertEqual(final['phase'], 'failed')
        self.assertEqual(final['sdk']['returncode'], -signal.SIGKILL)

    def test_plain_nullable_fields_must_be_explicit_and_unknown_owner_fields_refuse(self) -> None:
        runtime = self.root / 'plain-schema'
        with launch.application(self.policy, runtime, plain=True) as process:
            self.assertEqual(benchmark.batch(process, 10)['checksum'], 41284864)
        path = runtime / 'python-execution.json'
        original = path.read_bytes()
        for key in ('sdk', 'bootstrap_recheck'):
            value = json.loads(original)
            del value[key]
            path.write_text(json.dumps(value))
            with self.subTest(missing=key), self.assertRaises(ValueError):
                launch.completed(process, runtime, self.policy, plain=True)
        value = json.loads(original)
        value['foreign_field'] = 'unqualified schema change'
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            launch.completed(process, runtime, self.policy, plain=True)
        path.write_bytes(original)
        launch.completed(process, runtime, self.policy, plain=True)
