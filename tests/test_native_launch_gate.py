"""The native CI gate must reject skipped or incomplete execution evidence."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ci import check_native_launch as gate
from ci import native_launch_suite as worker


class NativeLaunchGateTests(unittest.TestCase):
    def test_suite_report_must_be_complete_successful_and_have_no_skips(self) -> None:
        good = {'selected': 14, 'tests_run': 14, 'skipped': 0, 'failures': 0, 'errors': 0, 'successful': True}
        gate.test_result(good)
        for changed in (None, [], good | {'selected': True}, good | {'selected': 0},
                        good | {'tests_run': 13}, good | {'skipped': 1}, good | {'errors': 1},
                        good | {'failures': 1}, good | {'successful': False}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                gate.test_result(changed)

    def test_actual_unittest_skip_and_empty_suite_cannot_pass_the_worker(self) -> None:
        def passed() -> None:
            """A real no-op unittest fixture; no native execution is claimed here."""

        def skipped() -> None:
            raise unittest.SkipTest('deliberate gate regression')

        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / 'result.json'
            for count, skip, expected in ((8, False, 0), (8, True, 1), (0, False, 1)):
                suite = unittest.TestSuite(unittest.FunctionTestCase(skipped if skip and index == 0 else passed)
                                           for index in range(count))
                with patch('unittest.defaultTestLoader.loadTestsFromNames', return_value=suite), \
                        patch.dict(os.environ, OTELC_NATIVE_TEST_REPORT=str(report)), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(worker.main(), expected)
                value = json.loads(report.read_text())
                self.assertEqual(value['tests_run'], count)
                self.assertEqual(value['skipped'], int(skip))

    def test_make_check_runs_the_actual_launcher_and_each_component_floor(self) -> None:
        make = (gate.ROOT / 'Makefile').read_text()
        self.assertIn('test: java-build node-observer node-http native-check native-launch-check', make)
        self.assertIn('-m ci.check_native_launch', make)
        for source in ('otelc/native_launch.py', 'ci/check_native_launch.py', 'ci/native_launch_suite.py'):
            self.assertIn("--include='*/" + source + "' --fail-under=80", make)
