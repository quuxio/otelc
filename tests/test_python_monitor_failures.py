"""Optional monitor failures must not replace application results or exceptions."""

from __future__ import annotations

import sys
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest import mock

from otelc.config import Policy
from otelc.python_monitor import PythonMonitor


class PythonMonitorFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.runtime = mock.Mock()
        self.lifetimes = mock.Mock()
        self.monitor = PythonMonitor(Policy.parse({'schema_version': 3}),
            Path(temporary.name), self.runtime, lifetimes=self.lifetimes)

    def test_unavailable_frame_is_loss_for_every_callback_without_alternative_lookup(self) -> None:
        code = self.test_unavailable_frame_is_loss_for_every_callback_without_alternative_lookup.__code__
        callbacks = ((self.monitor._start, (code, 0)),
            (self.monitor._return, (code, 0, 42)),
            (self.monitor._unwind, (code, 0, ValueError('original'))),
            (self.monitor._yield, (code, 0, 42)),
            (self.monitor._resume, (code, 0)),
            (self.monitor._call, (code, 0, object, None)),
            (self.monitor._c_return, (code, 0, object, None)),
            (self.monitor._c_raise, (code, 0, object, None)))
        for callback, arguments in callbacks:
            with self.subTest(callback=callback.__name__):
                self.runtime.reset_mock()
                self.lifetimes.reset_mock()
                with mock.patch.object(self.monitor, 'selected', return_value='fixture.workload'), \
                     mock.patch('otelc.python_monitor.sys._getframe', side_effect=RuntimeError('denied')) as lookup:
                    callback(*arguments)
                lookup.assert_called_once_with(2)
                self.runtime.reject.assert_called_once_with('monitor_frame')
                self.runtime.abandon.assert_not_called()
                self.lifetimes.assert_not_called()
                self.assertEqual(self.lifetimes.method_calls, [])

    def test_cleanup_failure_does_not_replace_original_probe_failure(self) -> None:
        frame = sys._getframe()
        self.monitor._names[id(frame.f_code)] = (weakref.ref(frame.f_code), 'fixture.workload')
        self.runtime.abandon.side_effect = RuntimeError('cleanup unavailable')
        for operation, callback in ((self.runtime.leave, lambda: self.monitor._complete(frame, None)),
                (self.runtime.suspend, lambda: self.monitor._suspend(frame, resume=False)),
                (self.runtime.resume, lambda: self.monitor._suspend(frame, resume=True))):
            with self.subTest(operation=operation):
                operation.side_effect = ValueError('probe unavailable')
                callback()
                self.runtime.abandon.assert_called_with(id(frame))
        self.assertEqual(self.runtime.reject.call_args_list.count(mock.call('monitor_cleanup')), 3)
        self.assertIn(mock.call('monitor_completion'), self.runtime.reject.call_args_list)
        self.assertEqual(self.runtime.reject.call_args_list.count(mock.call('monitor_context')), 2)

    def test_failed_selection_has_no_frame_key_to_abandon(self) -> None:
        with mock.patch.object(self.monitor, 'selected', side_effect=ValueError('selection')):
            self.monitor._start(self.test_failed_selection_has_no_frame_key_to_abandon.__code__, 0)
        self.runtime.reject.assert_called_once_with('monitor_start')
        self.runtime.abandon.assert_not_called()
