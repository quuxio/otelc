"""The native floor uses actual execution and refuses missing or weak evidence."""

from __future__ import annotations

import copy
import sys
import unittest

from ci import check_python_inputs_coverage as gate


class PythonInputCoverageTests(unittest.TestCase):
    def test_real_native_profiles_cover_each_component_and_retain_branch_counts(self) -> None:
        result = gate.check()
        self.assertEqual(result['version'], sys.version)
        self.assertEqual(set(result['sources']), set(gate.NATIVE_SOURCES))
        for summary in result['sources'].values():
            self.assertGreater(summary['lines']['count'], 0)
            self.assertGreaterEqual(summary['lines']['percent'], 80)
            self.assertGreater(summary['branches']['count'], 0)

    def test_low_empty_missing_or_duplicate_actual_sources_cannot_pass(self) -> None:
        rows = [{'filename': str(gate.ROOT / 'native/python' / name),
                 'summary': {'lines': {'count': 1, 'percent': 100}}} for name in gate.NATIVE_SOURCES]
        self.assertEqual(set(gate.summaries({'data': [{'files': rows}]})), set(gate.NATIVE_SOURCES))
        for change in ({'count': 0, 'percent': 100}, {'count': 10, 'percent': 79}):
            altered = copy.deepcopy(rows)
            altered[0]['summary']['lines'] = change
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'below80'):
                gate.summaries({'data': [{'files': altered}]})
        for altered in (rows[1:], rows + [rows[0]]):
            with self.subTest(size=len(altered)), self.assertRaisesRegex(ValueError, 'identify'):
                gate.summaries({'data': [{'files': altered}]})
