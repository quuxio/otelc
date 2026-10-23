"""Force GC at a real cache allocation while another GC callback owns its queue."""
import faulthandler
import gc
import os
from pathlib import Path
import sys
import threading
import weakref

root = Path(sys.argv[1])
from otelc.config import Policy
from otelc.python_lifetimes import Collected
from otelc.python_monitor import PythonMonitor
from otelc.python_runtime import PythonRuntime
from otelc.python_sdk_process import SdkProcess

faulthandler.dump_traceback_later(7, exit=True)
gc.disable()
application = root / 'application'
application.mkdir()
filename = application / 'entry.py'
filename.write_text('class WorkItem:\n def __init__(self,cycle=False):\n  if cycle:self.cycle=self\ndef unselected():return 7\n')
namespace = {'__name__': 'fixture'}
exec(compile(filename.read_text(), str(filename), 'exec'), namespace)
policy = Policy.parse({'schema_version': 3, 'sources': {'include': ['entry.py']},
    'functions': {'include': ['*never_selected*']}, 'metrics': {'collection': True, 'emission': False},
    'traces': {'enabled': False}, 'lifetimes': {'enabled': True, 'include': ['*WorkItem']},
    'export': {'interval_ms': 100000}})
sdk = SdkProcess.start(policy, root / 'sdk-runtime')
runtime = PythonRuntime(policy, sdk.channel, root=application)
monitor = PythonMonitor(policy, application, runtime, lifetimes=runtime.lifetimes)
ready, release = threading.Event(), threading.Event()
worker = None
original_rejection = monitor._cache_rejection

def cache_rejection(code):
    if code is Collected.__init__.__code__ and threading.get_ident() == worker.ident:
        ready.set()
        assert release.wait(4)
    return original_rejection(code)

monitor._cache_rejection = cache_rejection
monitor.install()
first = [namespace['WorkItem'](True)]
second = [namespace['WorkItem']()]
assert runtime.lifetimes.admitted == 2

def collect_second():
    second.clear()

worker = threading.Thread(target=collect_second, daemon=True)
worker.start()
assert ready.wait(3), 'did not reach monitored Collected construction with the pending lock held'
first.clear()
target = namespace['unselected'].__code__
original_reference = weakref.ref

def reference(code, callback=None):
    if code is target:
        assert monitor._rejected_lock._is_owned()
        release.set()
        gc.collect()
    return original_reference(code, callback)

try:
    weakref.ref = reference
    assert namespace['unselected']() == 7
    worker.join(2)
    assert not worker.is_alive()
    print('completed cache/GC overlap', flush=True)
finally:
    weakref.ref = original_reference
    monitor.close()
    runtime.close()
    report = sdk.wait_report()
    assert report['phase'] == 'closed'
    assert report['producer']['lifetimes']['gc_completed'] == 2
    sdk.abort()
    faulthandler.cancel_dump_traceback_later()
