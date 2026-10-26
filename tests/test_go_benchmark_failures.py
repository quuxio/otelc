"""Failure routing and output boundaries; no compiler or SDK qualification."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from otelc import go_benchmark as adapter
from otelc.config import Policy


class GoBenchmarkFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.sdk = self.base / 'SDK'
        self.root = self.sdk / 'examples/go'
        self.root.mkdir(parents=True)
        self.build = self.base / 'Build'
        self.build.mkdir()
        self.policy = Policy.parse({'schema_version': 3})
        self.receipt = {'receipt': {'root': str(self.root), 'target': '.'}}

    def test_every_cleanup_step_runs_without_replacing_the_body_failure(self) -> None:
        owner = Mock(pid=1234)
        process = adapter.Application(owner, 5678, 0)
        original = KeyboardInterrupt('measurement cancelled')
        owner.stdin.close.side_effect = OSError('input close failed')
        owner.stdout.close.side_effect = OSError('output close failed')
        with patch.object(adapter.benchmark, 'stop', side_effect=ValueError('application stop failed')) as stop, \
                patch.object(adapter, 'stop_group', side_effect=TimeoutError('group stop failed')) as group:
            adapter.shutdown(owner, process, original)
        stop.assert_called_once_with(process); group.assert_called_once_with(owner)
        owner.stdin.close.assert_called_once(); owner.stdout.close.assert_called_once()
        self.assertEqual(original.__notes__, ['Go benchmark application stop also failed: ValueError',
            'Go benchmark owner stop also failed: TimeoutError', 'Go benchmark stdin close also failed: OSError',
            'Go benchmark stdout close also failed: OSError'])

    def alias_for(self, path: Path) -> Path:
        alias = path.with_name(path.name.swapcase())
        if not alias.exists():
            alias.symlink_to(path, target_is_directory=True)
        self.assertTrue(alias.samefile(path))
        return alias

    def test_report_refuses_source_and_build_aliases_before_creating_output(self) -> None:
        roots = (self.root, self.alias_for(self.root), self.build, self.alias_for(self.build))
        for index, root in enumerate(roots):
            output = root / f'results-{index}'
            with self.subTest(root=root):
                with patch.object(adapter.prepared, 'ROOT', self.sdk), \
                        patch.object(adapter.prepared, 'verify_build', return_value=self.receipt), \
                        patch.object(adapter, 'measure', side_effect=AssertionError('measurement sentinel')), \
                        self.assertRaisesRegex(ValueError, 'outside original sources and retained build'):
                    adapter.run(self.policy, output, build_directory=self.build, publish=False)
                self.assertFalse(output.exists())

    def test_normal_external_and_shared_prefix_outputs_reach_measurement(self) -> None:
        for output in (self.base / 'reports/result', self.sdk / 'examples/go-copy/result'):
            with self.subTest(output=output), patch.object(adapter.prepared, 'ROOT', self.sdk), \
                    patch.object(adapter.prepared, 'verify_build', return_value=self.receipt), \
                    patch.object(adapter, 'measure', side_effect=AssertionError('measurement sentinel')), \
                    self.assertRaisesRegex(AssertionError, 'measurement sentinel'):
                adapter.run(self.policy, output, build_directory=self.build, publish=False)
            self.assertTrue(output.is_dir())

    def test_measurement_error_or_interrupt_survives_failed_final_validation(self) -> None:
        cases = ((RuntimeError('measurement failed'), None), (KeyboardInterrupt(), None),
                 (RuntimeError('measurement failed'), OSError('pipe close failed')))
        for index, (original, cleanup_error) in enumerate(cases):
            with self.subTest(error=type(original).__name__):
                owner = Mock(pid=1234)
                process = adapter.Application(owner, 5678, 0)
                runtime = self.base / str(index)
                runtime.mkdir(mode=0o700)
                with patch.object(adapter.prepared, 'prepare'), \
                        patch.object(adapter.prepared, 'command', return_value=['not-executed']), \
                        patch.object(adapter.subprocess, 'Popen', return_value=owner), \
                        patch.object(adapter, 'started', return_value=process), \
                        patch.object(adapter, 'shutdown', side_effect=cleanup_error) as shutdown, \
                        patch.object(adapter, 'completed', side_effect=ValueError('SDK drain failed')) as completed, \
                        self.assertRaises(type(original)) as caught:
                    with adapter.application(self.policy, runtime, self.build, plain=True):
                        raise original
                self.assertIs(caught.exception, original)
                self.assertTrue(any('ValueError' in note for note in original.__notes__))
                if cleanup_error is not None:
                    self.assertTrue(any('OSError' in note for note in original.__notes__))
                shutdown.assert_called_once_with(owner, process, original)
                completed.assert_called_once_with(process, runtime, plain=True, retain=False)

    def test_first_cleanup_error_survives_a_second_final_validation_failure(self) -> None:
        owner = Mock(pid=1234)
        process = adapter.Application(owner, 5678, 0)
        runtime = self.base / 'runtime'
        runtime.mkdir(mode=0o700)
        original = OSError('pipe close failed')
        with patch.object(adapter.prepared, 'prepare'), \
                patch.object(adapter.prepared, 'command', return_value=['not-executed']), \
                patch.object(adapter.subprocess, 'Popen', return_value=owner), \
                patch.object(adapter, 'started', return_value=process), \
                patch.object(adapter, 'shutdown', side_effect=original), \
                patch.object(adapter, 'completed', side_effect=ValueError('SDK drain failed')) as completed, \
                self.assertRaises(OSError) as caught:
            with adapter.application(self.policy, runtime, self.build, plain=True):
                pass
        self.assertIs(caught.exception, original)
        self.assertTrue(any('ValueError' in note for note in original.__notes__))
        completed.assert_called_once_with(process, runtime, plain=True, retain=False)

    def test_failed_final_validation_without_an_earlier_error_still_fails(self) -> None:
        owner = Mock(pid=1234)
        process = adapter.Application(owner, 5678, 0)
        runtime = self.base / 'runtime'
        runtime.mkdir(mode=0o700)
        original = ValueError('SDK drain failed')
        with patch.object(adapter.prepared, 'prepare'), \
                patch.object(adapter.prepared, 'command', return_value=['not-executed']), \
                patch.object(adapter.subprocess, 'Popen', return_value=owner), \
                patch.object(adapter, 'started', return_value=process), \
                patch.object(adapter, 'shutdown'), \
                patch.object(adapter, 'completed', side_effect=original) as completed, \
                self.assertRaises(ValueError) as caught:
            with adapter.application(self.policy, runtime, self.build, plain=True):
                pass
        self.assertIs(caught.exception, original)
        completed.assert_called_once_with(process, runtime, plain=True, retain=True)
