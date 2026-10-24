"""Run the explicit native integration suite and retain real skip/failure counts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """Fail if any selected launcher test is skipped, missing or unsuccessful."""
    sys.path.insert(0, str(ROOT / 'tests'))
    suite = unittest.defaultTestLoader.loadTestsFromNames(
        ['test_native_launch', 'test_native_launch_tooling'])
    selected = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    value = {'selected': selected, 'tests_run': result.testsRun,
             'skipped': len(result.skipped), 'failures': len(result.failures),
             'errors': len(result.errors), 'successful': result.wasSuccessful()}
    Path(os.environ['OTELC_NATIVE_TEST_REPORT']).write_text(json.dumps(value, indent=2) + '\n')
    return 0 if selected >= 8 and result.testsRun == selected and not result.skipped and result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
