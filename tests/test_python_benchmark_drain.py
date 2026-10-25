"""Benchmark drain validation must survive errors in the measured body."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from otelc import python_benchmark
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]


class PythonBenchmarkDrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy.load(ROOT / 'examples/otelc.toml')
        self.runtime = Path('/unused-drain-test')
        self.process = SimpleNamespace(pid=123, sdk_pid=456, sdk_report=None)
        self.closed = False

    @contextmanager
    def application(self, *args, **kwargs):
        try:
            yield self.process
        finally:
            self.process.sdk_report = {'retained_after_shutdown': True}
            self.closed = True

    def validate(self, report, pid, sdk_pid, *, lifetimes):
        self.assertTrue(self.closed)
        self.assertIs(report, self.process.sdk_report)
        self.assertEqual((pid, sdk_pid), (123, 456))
        self.assertEqual(lifetimes, self.policy.data['lifetimes']['enabled'])

    def test_normal_exit_validates_only_after_owned_shutdown(self) -> None:
        with mock.patch.object(python_benchmark.launch, 'application', self.application), \
                mock.patch.object(python_benchmark, 'validate_drain', side_effect=self.validate) as validate:
            with python_benchmark.application(self.policy, self.runtime, plain=False) as (process, sdk_pid):
                self.assertIs(process, self.process)
                self.assertEqual(sdk_pid, 456)
                validate.assert_not_called()
            validate.assert_called_once()

    def test_body_error_still_validates_and_preserves_original_exception(self) -> None:
        failure = RuntimeError('controlled measured-body failure')
        with mock.patch.object(python_benchmark.launch, 'application', self.application), \
                mock.patch.object(python_benchmark, 'validate_drain', side_effect=self.validate) as validate:
            with self.assertRaises(RuntimeError) as caught:
                with python_benchmark.application(self.policy, self.runtime, plain=False):
                    raise failure
            self.assertIs(caught.exception, failure)
            validate.assert_called_once()

    def test_bad_drain_after_body_error_keeps_both_failures(self) -> None:
        failure = RuntimeError('controlled measured-body failure')
        drain_failure = ValueError('controlled invalid SDK drain')
        with mock.patch.object(python_benchmark.launch, 'application', self.application), \
                mock.patch.object(python_benchmark, 'validate_drain', side_effect=drain_failure) as validate:
            with self.assertRaises(RuntimeError) as caught:
                with python_benchmark.application(self.policy, self.runtime, plain=False):
                    raise failure
            self.assertIs(caught.exception, failure)
            self.assertIn('Python benchmark final validation also failed: ValueError', failure.__notes__)
            self.assertTrue(self.closed)
            validate.assert_called_once()

    def test_failed_enter_never_validates_an_unavailable_process(self) -> None:
        failure = OSError('controlled failed launch')
        manager = mock.MagicMock()
        manager.__enter__.side_effect = failure
        with mock.patch.object(python_benchmark.launch, 'application', return_value=manager), \
                mock.patch.object(python_benchmark, 'validate_drain') as validate:
            with self.assertRaises(OSError) as caught:
                with python_benchmark.application(self.policy, self.runtime, plain=False):
                    self.fail('failed entry must not yield')
            self.assertIs(caught.exception, failure)
            validate.assert_not_called()

    def test_normal_exit_without_a_report_cannot_skip_validation(self) -> None:
        manager = mock.MagicMock()
        manager.__enter__.return_value = self.process
        with mock.patch.object(python_benchmark.launch, 'application', return_value=manager):
            with self.assertRaisesRegex(ValueError, 'SDK drain is not verified'):
                with python_benchmark.application(self.policy, self.runtime, plain=False):
                    pass

    def test_stock_plain_error_has_no_sdk_drain_to_validate(self) -> None:
        failure = RuntimeError('controlled stock-body failure')
        with mock.patch.object(python_benchmark.stock, 'application', self.application), \
                mock.patch.object(python_benchmark, 'validate_drain') as validate:
            with self.assertRaises(RuntimeError) as caught:
                with python_benchmark.application(self.policy, self.runtime, plain=True):
                    raise failure
            self.assertIs(caught.exception, failure)
            self.assertTrue(self.closed)
            validate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
