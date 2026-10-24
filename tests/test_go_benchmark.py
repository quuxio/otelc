"""Actual Go benchmarks retain real process costs and refuse incomplete evidence."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ci import build_go_upstream as tooling
from otelc import benchmark, go_benchmark as adapter, go_build, prepared_go as prepared
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class GoBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='go-benchmark-', dir=ROOT / 'build')
        cls.base = Path(cls.temporary.name).resolve()
        cls.build = cls.base / 'build'
        cls.common = Policy.load(ROOT / 'examples/otelc.toml')
        cls.original = go_build.inputs(ROOT / 'examples/go')
        tools = tooling.build('go', ROOT / 'build/go-upstream')
        go_build.build_project(ROOT / 'examples/go', '.', cls.common, cls.build, tools)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        data = copy.deepcopy(self.common.data)
        data['benchmark'].update(iterations=10, samples=2, warmup=1)
        data['metrics'].update(collection=False, emission=False)
        data['traces']['sample_ratio'] = 1.0
        data['export'].update(endpoint=f'http://127.0.0.1:{self.capture.server.server_port}', interval_ms=100000)
        self.policy = Policy.parse(data)
        self.output = self.base / self._testMethodName

    def test_five_modes_have_real_embedded_sdk_costs_and_decoded_metrics(self) -> None:
        report = adapter.run(self.policy, self.output, build_directory=self.build, comparison_id='shared-go')
        self.assertEqual((report['language'], report['comparison_id'], report['expected_checksum']),
                         ('go', 'shared-go', 41284864))
        self.assertEqual(report['launch_profile'], prepared.PROFILE)
        self.assertFalse(report['source_complete'])
        self.assertEqual(report['sdk_pid'], report['pids']['instrumented'])
        self.assertEqual({row['pid'] for row in report['transitions']}, {report['sdk_pid']})
        self.assertEqual(set(report['samples_ns_per_operation']), {'plain', *benchmark.STATES})
        self.assertTrue(all(len(values) == 2 for values in report['samples_ns_per_operation'].values()))
        self.assertEqual(go_build.inputs(ROOT / 'examples/go'), self.original)
        for lane in ('plain', 'instrumented'):
            process = report['pids'][lane]
            owner = report['supervisor_pids'][lane]
            self.assertNotEqual(process, owner)
            for pid in (process, owner):
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            execution = prepared.execution(self.output / lane, supervisor_pid=owner,
                                           parent_pid=os.getpid(), returncode=0)
            self.assertEqual(execution['application_exit_code'], 0)
            self.assertEqual(execution['failures'], {})
            self.assertEqual(execution['sdk_pid'], None if lane == 'plain' else process)
            self.assertEqual(report['launch_to_first_response'][lane]['response']['checksum'], benchmark.checksum(1))
            for name, expected in report['launch_evidence'][lane].items():
                self.assertEqual(adapter.identity(self.output / lane / name), expected)
        raw = report['process_samples']
        self.assertEqual(len(raw), 10)
        self.assertEqual(len((self.output / 'responses.jsonl').read_text().splitlines()), 10)
        for row in raw:
            lane = 'plain' if row['mode'] == 'plain' else 'instrumented'
            self.assertEqual(row['pid'], report['pids'][lane])
            self.assertEqual(set(row['process_usage']), {'application', 'supervisor'})
            self.assertEqual(row['checksum'], report['expected_checksum'])
            for role, usage in row['process_usage'].items():
                expected = row['pid'] if role == 'application' else report['supervisor_pids'][lane]
                self.assertEqual((usage['before']['pid'], usage['after']['pid']), (expected, expected))
                self.assertGreaterEqual(usage['cpu_ns'], 0)
                self.assertGreater(usage['rss_bytes'], 0)
        health = prepared.read(self.output / 'instrumented/health.json')
        self.assertEqual(health['probe_accounting']['function_ended'], '91')
        self.assertEqual(health['probe_accounting']['resource_released'], '91')
        self.assertEqual(self.capture.calls(), 40)
        self.assertEqual(len(self.capture.points('otelc.benchmark.operation.duration')), 5)
        self.assertTrue(self.capture.points('otelc.benchmark.process.cpu.duration'))
        self.assertTrue(self.capture.points('otelc.benchmark.process.rss'))
        self.assertEqual(len(self.capture.points('otelc.benchmark.launch_to_first_response')), 2)

    def test_failed_final_sdk_validation_keeps_raw_samples_without_publication(self) -> None:
        checked = prepared.execution

        def execution(*args: object, **kwargs: object) -> dict:
            result = checked(*args, **kwargs)
            if result['sdk_health'] is not None:
                result['sdk_health']['probe_accounting']['lifetime_lost'] = '1'
            return result

        with patch.object(prepared, 'execution', side_effect=execution):
            with self.assertRaisesRegex(ValueError, 'final SDK drain'):
                adapter.run(self.policy, self.output, build_directory=self.build)
        self.assertEqual(len((self.output / 'raw.jsonl').read_text().splitlines()), 10)
        self.assertFalse((self.output / 'report.json').exists())
        self.assertFalse(self.capture.points('otelc.benchmark.operation.duration'))

    def test_body_and_metadata_failures_still_close_and_validate_owned_application(self) -> None:
        failure = RuntimeError('controlled body failure')
        observed = []
        runtime = self.output
        with self.assertRaises(RuntimeError) as caught:
            with adapter.application(self.policy, runtime, self.build, plain=False) as process:
                observed.append(process)
                self.assertEqual(benchmark.batch(process, 1)['checksum'], benchmark.checksum(1))
                raise failure
        self.assertIs(caught.exception, failure)
        self.assertIsNotNone(observed[0].poll())
        self.assertEqual(prepared.read(runtime / 'go-execution.json')['failures'], {})
        write = adapter.files.write

        def failed_write(path: Path, value: dict) -> None:
            if path.name == 'process.json':
                raise OSError('controlled process evidence failure')
            write(path, value)

        runtime = self.base / (self._testMethodName + '-publication')
        with patch.object(adapter.files, 'write', side_effect=failed_write):
            with self.assertRaisesRegex(OSError, 'controlled process evidence failure'):
                with adapter.application(self.policy, runtime, self.build, plain=False):
                    self.fail('failed evidence must prevent measurement')
        self.assertEqual(prepared.read(runtime / 'go-execution.json')['failures'], {})

    def test_startup_owner_timeout_exit_and_bad_identity_are_visible(self) -> None:
        owner = Mock(pid=os.getpid(), poll=Mock(return_value=None))
        with patch.object(prepared, 'verify', return_value=({}, {}, {})), \
                patch.object(prepared, 'read', side_effect=FileNotFoundError):
            with self.assertRaises(TimeoutError):
                adapter.started(owner, self.output, 1, plain=True, timeout=0)
            owner.poll.return_value = 7
            with self.assertRaises(RuntimeError):
                adapter.started(owner, self.output, 1, plain=True)
        request = {'launch_id': 'a' * 32}
        plan = {'argv': [], 'binary': {}}
        with patch.object(prepared, 'verify', return_value=(request, plan, {})), \
                patch.object(prepared, 'read', return_value={'pid': owner.pid}):
            with self.assertRaises(ValueError):
                adapter.started(owner, self.output, 1, plain=True)

    def test_wrong_build_and_changed_retained_evidence_prevent_report(self) -> None:
        built = prepared.verify_build(self.build, self.policy)
        changed = copy.deepcopy(built)
        changed['receipt']['root'] = '/not-the-shared-example'
        with patch.object(prepared, 'verify_build', return_value=changed), \
                patch('subprocess.Popen', side_effect=AssertionError('no application permitted')):
            with self.assertRaisesRegex(ValueError, 'unchanged shared'):
                adapter.run(self.policy, self.output, build_directory=self.build)
        self.assertFalse(self.output.exists())
        measured = adapter.measure

        def changed_after_lane(*args: object, **kwargs: object) -> adapter.Application:
            result = measured(*args, **kwargs)
            if args[3] == 'instrumented':
                (self.output / 'plain/go-first-response.json').write_text('{}')
            return result

        with patch.object(adapter, 'measure', side_effect=changed_after_lane):
            with self.assertRaisesRegex(ValueError, 'retained benchmark evidence changed'):
                adapter.run(self.policy, self.output, build_directory=self.build)
        self.assertFalse((self.output / 'report.json').exists())

    def test_wrong_live_state_and_unverified_completion_refuse(self) -> None:
        process = Mock(pid=123)
        with patch.object(adapter.control, 'request', return_value={'pid': 124}):
            with self.assertRaisesRegex(ValueError, 'metric state changed'):
                adapter.switch(self.output, process, 'both_off')
        with patch.object(prepared, 'execution', return_value={'application_pid': 124}):
            with self.assertRaisesRegex(ValueError, 'execution is incomplete'):
                adapter.completed(process, self.output, plain=True)


if __name__ == '__main__':
    unittest.main()
