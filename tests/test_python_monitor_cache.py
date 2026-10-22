"""Rejected code must not repeat filesystem work or become a retained object."""

from __future__ import annotations

import gc
import tempfile
import unittest
import weakref
from pathlib import Path
from types import CodeType
from unittest import mock

from otelc.config import Policy
from otelc.python_monitor import PythonMonitor


class PythonMonitorCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runtime = mock.Mock()
        self.policy = Policy.parse({'schema_version': 3,
            'sources': {'include': ['*.py']},
            'functions': {'include': ['*workload']}})
        self.monitor = PythonMonitor(self.policy, self.root, self.runtime)
        self.addCleanup(self.monitor.close)

    def code(self, name: str) -> CodeType:
        return compile(f'def {name}(): return 7', str(self.root / 'entry.py'), 'exec').co_consts[0]

    def test_repeated_rejected_calls_resolve_the_source_once(self) -> None:
        code = self.code('unselected')
        original = Path.resolve
        calls = []

        def resolve(path, *args, **kwargs):
            calls.append(path)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'resolve', autospec=True, side_effect=resolve):
            for _ in range(100):
                self.assertIsNone(self.monitor.selected(code))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.monitor.selected(self.code('workload')), 'entry.workload')
        self.runtime.reject.assert_not_called()

    def test_rejected_code_is_weak_and_its_entry_is_released(self) -> None:
        code = self.code('unselected')
        reference = weakref.ref(code)
        self.monitor.selected(code)
        self.assertEqual(len(self.monitor._rejected), 1)
        del code
        gc.collect()
        self.assertIsNone(reference())
        self.assertFalse(self.monitor._rejected)

    def test_full_cache_preserves_selection_without_retaining_excess_code(self) -> None:
        codes = [self.code(f'excluded_{number}') for number in range(3)]
        with mock.patch('otelc.python_monitor.MAX_REJECTED_CODES', 2):
            for code in codes:
                self.assertIsNone(self.monitor.selected(code))
            self.assertEqual(len(self.monitor._rejected), 2)
            self.assertIsNone(self.monitor.selected(codes[-1]))
            selected = self.code('workload')
            self.assertEqual(self.monitor.selected(selected), 'entry.workload')
            self.assertIs(self.monitor._names[id(selected)][0], selected)
        self.runtime.reject.assert_not_called()

    def test_stale_identity_entry_cannot_hide_a_different_selected_code(self) -> None:
        old = self.code('unselected')
        selected = self.code('workload')
        self.monitor._rejected[id(selected)] = weakref.ref(old)
        self.assertEqual(self.monitor.selected(selected), 'entry.workload')

    def test_path_failure_is_not_cached_as_a_successful_exclusion(self) -> None:
        code = self.code('unselected')
        with mock.patch.object(Path, 'resolve', side_effect=OSError('controlled path failure')) as resolve:
            for _ in range(2):
                with self.assertRaisesRegex(OSError, 'controlled path failure'):
                    self.monitor.selected(code)
        self.assertEqual(resolve.call_count, 2)
        self.assertIsNone(self.monitor.selected(code))

    def test_close_releases_the_rejection_cache_without_installing_monitoring(self) -> None:
        self.monitor.selected(self.code('unselected'))
        code = self.code('also_unselected')
        self.monitor.selected(code)
        self.monitor.close()
        self.assertFalse(self.monitor._rejected)
