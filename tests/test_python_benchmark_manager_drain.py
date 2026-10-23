"""Keep real launch/completion control flow while simulating process boundaries."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from otelc import python_benchmark as subject
from otelc.config import Policy
from otelc.file_inputs import identity
from test_python_input_journal import final, source

ROOT = Path(__file__).resolve().parents[1]


class PythonBenchmarkManagerDrainTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name) / 'runtime'
        self.runtime.mkdir(mode=0o700)
        self.policy = Policy.load(ROOT / 'examples/otelc.toml')
        self.events = []
        self.owner = SimpleNamespace(pid=123, stdin=mock.Mock(), stdout=mock.Mock())
        self.process = subject.launch.Application(self.owner, 321, 456,
                                                  {'sdk_launch_id': 'a' * 32})
        self.bindings = {'supervisor_pid': 123, 'parent_pid': os.getpid(), 'process_group': 123,
                         'request_sha256': '1' * 64, 'plan_sha256': '2' * 64,
                         'prepared_sha256': '3' * 64}
        entry = ROOT / 'examples/python/BenchmarkApp.py'
        content = entry.read_bytes()
        journal = self.runtime / 'python-inputs.journal'
        journal.write_bytes(source(pid=321, content=content, path=os.fsencode(entry))
                            + final(pid=321, size=len(content)))
        journal.chmod(0o600)
        self.execution = {**self.bindings, 'schema_version': 1, 'kind': 'otelc.python.execution',
            'phase': 'closed', 'plain': False, 'source_complete': False, 'failures': {},
            'input_recheck': True, 'bootstrap_recheck': True,
            'application': {'pid': 321, 'returncode': 0},
            'sdk': {'pid': 456, 'returncode': 0, 'phase': 'closed', 'launch_id': 'a' * 32},
            'entry_evidence': {'phase': 'complete', 'python_finalized': True,
                'capture_failures': 0, 'journal_failures': 0, 'journal': identity(journal)}}
        for name in (subject.launch.prepared.REQUEST, subject.launch.prepared.PLAN,
                     subject.launch.prepared.PREPARED, 'python-application-start.json'):
            self.write(name, {})
        self.write('python-execution.json', self.execution)
        report = {'schema_version': 1, 'kind': 'otelc.python.sdk', 'phase': 'closed',
            'sdk_phase': 'closed', 'pid': 321, 'sdk_pid': 456, 'launch_id': 'a' * 32,
            'policy_sha256': subject.policy_digest(self.policy),
            'producer_checkpoint_available': True, 'shutdown_completed': True,
            'shutdown_complete': True, 'shutdown_failed': False, 'operation_pending': False,
            **{key: 0 for key in ('handoff_incomplete', 'protocol_errors', 'reply_lost',
                'operation_failures', 'export_failed', 'in_flight_exports', 'flush_failed',
                'in_flight_flushes')},
            'producer': {'active_functions': 0, 'runtime_losses': {'lost': 0},
                'handoff': {'attempted': 0, 'accepted': 0, 'lost': 0},
                'span_observation': {'sampled_started': 0, 'sampled_ended': 0, 'handed_off': 0},
                'lifetimes': {'lost': 0}},
            'traces': {'failed': 0, 'pending': 0},
            'metric_collection': {'failed': 0, 'pending': 0},
            'span_accounting': {'received': 0, 'sdk_exported': 0, 'queue_pending': 0,
                'queue_dropped': 0, 'queue_evidence': 'derived_after_shutdown'}}
        (self.runtime / 'sdk').mkdir(mode=0o700)
        self.write('sdk/python-sdk-status.json', report)

    def write(self, name: str, value: dict) -> None:
        path = self.runtime / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    @contextmanager
    def boundaries(self, *, stop_error=None, pipe_error=None, input_error=None):
        completed, validate = subject.launch.completed, subject.validate_drain

        def stop(process):
            self.events.append('application_stop')
            if stop_error is not None:
                raise stop_error

        def close():
            self.events.append('pipe_close')
            if pipe_error is not None:
                raise pipe_error

        def close_input():
            if input_error is not None:
                self.events.append('input_close_failed')
                raise input_error

        def completion(*args, **options):
            self.events.append('completed')
            return completed(*args, **options)

        def validation(*args, **options):
            self.events.append('validate_report' if self.process.sdk_report is not None else 'validate_none')
            return validate(*args, **options)

        self.owner.stdout.close.side_effect = close
        self.owner.stdin.close.side_effect = close_input
        with mock.patch.object(subject.launch.prepared, 'command', return_value=['unused-owner']), \
                mock.patch.object(subject.launch.subprocess, 'Popen', return_value=self.owner), \
                mock.patch.object(subject.launch, 'start', return_value=self.process), \
                mock.patch.object(subject.launch, 'bindings', return_value=self.bindings), \
                mock.patch.object(subject.launch.benchmark, 'stop', side_effect=stop), \
                mock.patch.object(subject.launch.prepared, 'stop_group',
                                  side_effect=lambda owner: self.events.append('owner_stop')), \
                mock.patch.object(subject.launch, 'completed', side_effect=completion), \
                mock.patch.object(subject, 'validate_drain', side_effect=validation):
            yield

    def run_body(self, failure=None) -> None:
        with subject.application(self.policy, self.runtime, plain=False):
            if failure is not None:
                raise failure

    def test_normal_exit_reads_and_validates_the_real_retained_completion(self) -> None:
        with self.boundaries():
            self.run_body()
        self.assertEqual(self.events, ['application_stop', 'owner_stop', 'pipe_close',
                                       'completed', 'validate_report'])
        self.assertEqual(len(self.process.evidence), 7)

    def test_body_failure_still_reads_completion_and_preserves_original_error(self) -> None:
        failure = RuntimeError('original measured-body failure')
        with self.boundaries():
            with self.assertRaises(RuntimeError) as caught:
                self.run_body(failure)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.events, ['application_stop', 'owner_stop', 'pipe_close',
                                       'completed', 'validate_report'])
        self.assertEqual(self.process.sdk_report['phase'], 'closed')

    def test_failed_completion_keeps_body_failure_without_validating_none(self) -> None:
        failure = RuntimeError('original measured-body failure')
        self.execution['phase'] = 'failed'
        self.write('python-execution.json', self.execution)
        with self.boundaries():
            with self.assertRaisesRegex(ValueError, 'completion is not verified') as caught:
                self.run_body(failure)
        self.assertIs(caught.exception.__context__, failure)
        self.assertEqual(self.events, ['application_stop', 'owner_stop', 'pipe_close', 'completed'])
        self.assertIsNone(self.process.sdk_report)

    def test_stop_failure_does_not_skip_completion_or_hide_the_body_failure(self) -> None:
        failure = RuntimeError('original measured-body failure')
        stop_error = OSError('cooperative stop failed')
        with self.boundaries(stop_error=stop_error):
            with self.assertRaises(RuntimeError) as caught:
                self.run_body(failure)
        self.assertIs(caught.exception, failure)
        self.assertIn('Prepared benchmark cleanup also failed: OSError', failure.__notes__)
        self.assertEqual(self.events[-2:], ['completed', 'validate_report'])
        self.assertEqual(self.process.evidence, {})

    def test_cleanup_failure_without_body_error_retains_drain_but_no_clean_evidence(self) -> None:
        stop_error = OSError('cooperative stop failed')
        with self.boundaries(stop_error=stop_error):
            with self.assertRaises(OSError) as caught:
                self.run_body()
        self.assertIs(caught.exception, stop_error)
        self.assertEqual(self.process.sdk_report['phase'], 'closed')
        self.assertEqual(self.process.evidence, {})
        self.assertEqual(self.events[-2:], ['completed', 'validate_report'])

    def test_pipe_failure_still_reads_completion_and_retains_original_chain(self) -> None:
        failure = RuntimeError('original measured-body failure')
        pipe_error = OSError('pipe close failed')
        with self.boundaries(pipe_error=pipe_error):
            with self.assertRaises(OSError) as caught:
                self.run_body(failure)
        self.assertIs(caught.exception, pipe_error)
        self.assertIs(caught.exception.__context__, failure)
        self.assertEqual(self.events[-2:], ['completed', 'validate_report'])

    def test_input_pipe_failure_still_closes_output_and_reads_final_evidence(self) -> None:
        failure = RuntimeError('original measured-body failure')
        pipe_error = OSError('input pipe close failed')
        with self.boundaries(input_error=pipe_error):
            with self.assertRaises(OSError) as caught:
                self.run_body(failure)
        self.assertIs(caught.exception, pipe_error)
        self.assertIs(caught.exception.__context__, failure)
        self.owner.stdout.close.assert_called_once()
        self.assertEqual(self.events, ['application_stop', 'owner_stop', 'input_close_failed',
                                       'pipe_close', 'completed', 'validate_report'])

    def test_both_pipe_failures_retain_the_complete_body_and_cleanup_chain(self) -> None:
        failure = RuntimeError('original measured-body failure')
        input_error, output_error = OSError('input pipe close failed'), OSError('output pipe close failed')
        with self.boundaries(input_error=input_error, pipe_error=output_error):
            with self.assertRaises(OSError) as caught:
                self.run_body(failure)
        self.assertIs(caught.exception, output_error)
        self.assertIs(output_error.__context__, input_error)
        self.assertIs(input_error.__context__, failure)
        self.assertEqual(self.events[-2:], ['completed', 'validate_report'])


if __name__ == '__main__':
    unittest.main()
