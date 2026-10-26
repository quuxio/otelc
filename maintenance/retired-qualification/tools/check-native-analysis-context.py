"""Run the focused native analysis regression against original and fixed imports."""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest

ROOT = Path('/Users/sclarke/github/otelc-qualification/work/native-analysis-component-context')
OUT = Path('/Users/sclarke/github/otelc-qualification/2026-10-26/native-analysis-component-context')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from tests import test_native_sonar as tests

if sys.argv[1] == 'before':
    original = subprocess.check_output(['git', 'show', 'f73f432a4131a686bb08969557bb2b10f3875546:ci/native_sonar.py'])
    module = types.ModuleType('original_native_sonar')
    module.__file__ = str(ROOT / 'ci/native_sonar.py')
    exec(compile(original, module.__file__, 'exec'), module.__dict__)
    tests.sonar = module
    suite = unittest.TestSuite([tests.NativeSonarTests('test_analysis_uses_each_coverage_producers_own_compilation_context')])
else:
    suite = unittest.defaultTestLoader.loadTestsFromModule(tests)
stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
log = stream.getvalue()
name = sys.argv[1] + '-' + str(sys.version_info.major) + str(sys.version_info.minor)
(OUT / (name + '.log')).write_text(log)
print(log)
if sys.argv[1] == 'before':
    assert result.testsRun == 1 and len(result.errors) == 1 and not result.failures
    assert 'ValueError: native compilation unit is duplicated' in result.errors[0][1]
else:
    assert result.wasSuccessful() and result.testsRun == 12
