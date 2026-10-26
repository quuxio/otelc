"""Run maintained new regressions against the unchanged prior runtime/CLI."""
from pathlib import Path
import sys,unittest
old=Path('/Users/sclarke/github/otelc-qualification/work/python-cancellation-current-main')
new=Path('/Users/sclarke/github/otelc-qualification/work/python-exception-name-encoding')
sys.path[:0]=[str(old),str(new/'tests'),str(old/'tests')]
import otelc.python_runtime as runtime
import test_python_cancellation as cancellation
assert Path(runtime.__file__)==old/'otelc/python_runtime.py'
assert Path(cancellation.__file__)==new/'tests/test_python_cancellation.py'
cancellation.ROOT=old
suite=unittest.defaultTestLoader.loadTestsFromNames(['test_python_exception_names','test_python_cancellation'])
result=unittest.TextTestRunner(verbosity=2).run(suite)
assert result.testsRun==4 and len(result.failures)==2 and len(result.errors)==3
print('Original exact57a runtime: three invalid-name failures and two real traces/combined CLI exit changes reproduced; final maintained tests only ROOT relocated, no frozen source edits.')
