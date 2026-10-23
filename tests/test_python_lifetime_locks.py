"""Real monitoring and GC callbacks must not invert SDK/runtime lock order."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_ORDER_APP = '''import faulthandler,gc,sys,threading,weakref
from pathlib import Path
from otelc.config import Policy
from otelc.python_runtime import PythonRuntime
from otelc.python_monitor import PythonMonitor
from otelc.python_sdk_process import SdkProcess
mode=sys.argv[1]; root=Path(sys.argv[2]); root.mkdir(mode=0o700)
faulthandler.dump_traceback_later(8,exit=True)
policy=Policy.parse({'schema_version':3,'sources':{'include':['*.py']},
    'metrics':{'collection':mode=='gc','emission':False},
    'traces':{'enabled':False},'lifetimes':{'enabled':True,'include':['*WorkItem']},
    'export':{'interval_ms':100000}})
sdk=SdkProcess.start(policy,root/'sdk')
try:
    runtime=PythonRuntime(policy,sdk.channel,root=root)
    monitor=PythonMonitor(policy,root,runtime,lifetimes=runtime.lifetimes)
    if mode!='probe':
        gc.disable()
        source=root/'app.py'
        source.write_text('class WorkItem:\\n    def __init__(self): self.loop=self\\n')
        scope={'__name__':'fixture'}
        exec(compile(source.read_text(),str(source),'exec'),scope)
        if mode=='gc':
            monitor.install()
            holder=[scope['WorkItem']()]
            monitor.close()
            assert runtime.lifetimes.admitted==1
            holder.clear()
        else:
            cls=scope.pop('WorkItem'); cls.loop=cls
            runtime.lifetimes.metadata(cls)
            reference=weakref.ref(cls); del cls
    held,release=threading.Event(),threading.Event()
    metric={'kind':'metric','scope':'fixture','instrument':'counter',
            'name':'fixture.lock.order','value':0}
    def other_writer():
        with sdk.channel.writer._lock:
            held.set(); release.wait()
            if mode=='probe': assert sdk.channel.writer.send(metric)
            else: gc.collect()
    worker=threading.Thread(target=other_writer,daemon=True)
    worker.start(); assert held.wait(2)
    with runtime._lock:
        if mode=='probe': monitor.install()
        release.set()
        assert sdk.channel.writer.send(metric)
    worker.join(2); assert not worker.is_alive()
    monitor.close()
    state=runtime.lifetimes.status()
    if mode=='gc': assert state['gc_completed']==1 and state['current_live']==0,state
    if mode=='type': assert reference() is None and state['tracked_constructors']==0,state
    runtime.close()
    report=sdk.wait_report(); assert report['phase']=='closed',report
finally:
    sdk.abort()
    faulthandler.cancel_dump_traceback_later()
print('completed real SDK drain')
'''


class PythonLifetimeLockTests(unittest.TestCase):
    def run_case(self, mode: str) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            environment = dict(os.environ, PYTHONPATH=str(ROOT))
            result = subprocess.run([sys.executable, '-c', LOCK_ORDER_APP, mode,
                str(Path(temporary).resolve() / 'runtime')], cwd=ROOT,
                env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, 'completed real SDK drain\n')
            self.assertEqual(result.stderr, '')

    def test_unselected_monitor_callbacks_under_writer_lock(self) -> None:
        self.run_case('probe')

    def test_object_gc_callback_under_writer_lock(self) -> None:
        self.run_case('gc')

    def test_class_gc_callback_under_writer_lock(self) -> None:
        self.run_case('type')
