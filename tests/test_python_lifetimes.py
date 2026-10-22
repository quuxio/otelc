"""Ordinary unchanged application classes drive real SDK lifetime observations."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from otelc import control
from test_java_integration import Capture, batch

ROOT = Path(os.environ.get('OTELC_LIFETIME_TEST_PROVIDER', Path(__file__).resolve().parents[1]))


class PythonLifetimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.application = self.root / 'application'
        self.application.mkdir()
        self.entry = self.application / 'entry.py'
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.config = self.root / 'otelc.toml'
        self.config.write_text(f'''schema_version=3
[sources]
include=["**/*.py"]
[functions]
include=["entry.workload"]
[lifetimes]
enabled=true
include=["entry.*"]
[traces]
enabled=true
sample_ratio=1.0
[export]
endpoint="http://127.0.0.1:{self.capture.server.server_port}"
interval_ms=100000
''')

    def run_app(self, source: str, *, loss: bool = False):
        self.entry.write_text(source)
        before = self.entry.read_bytes()
        baseline = subprocess.run([sys.executable, 'entry.py'], cwd=self.application,
                                  capture_output=True, text=True, timeout=10)
        runtime = self.root / 'runtime'
        result = subprocess.run([sys.executable, '-m', 'otelc', '--config', str(self.config),
            'python', '--root', str(self.application), '--entry', 'entry.py',
            '--runtime', str(runtime)], cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.stdout, baseline.stdout)
        expected_stderr = ('Python SDK drain failed or remains unverified\n' if loss else '')
        self.assertEqual(result.stderr, baseline.stderr + expected_stderr)
        self.assertEqual(self.entry.read_bytes(), before)
        report = json.loads((runtime / 'sdk/python-sdk-status.json').read_text())
        return result, report

    def lifetime_spans(self):
        return [span for request in self.capture.traces for resource in request.resource_spans
                for scope in resource.scope_spans if scope.scope.name == 'io.quux.otelc.lifetimes'
                for span in scope.spans]

    def test_success_failed_construction_release_retry_gc_cycles_and_parent_links(self) -> None:
        source = '''import gc,json,weakref
class Item:
    def __init__(this, fail=False):
        if fail: raise ValueError('original')
        this.failed=False
    def __hash__(this): raise AssertionError('do not hash')
    def __eq__(this, other): raise AssertionError('do not compare')
    def close(this):
        if not this.failed:
            this.failed=True
            raise ValueError('retry')
def workload():
    try: Item(True)
    except ValueError: pass
    item=Item()
    try: item.close()
    except ValueError: pass
    item.close();item.close()
    reference=weakref.ref(item)
    item.cycle=item
    del item
    gc.collect()
    return reference() is None
print(json.dumps({'collected':workload()}))
'''
        result, report = self.run_app(source)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['phase'], 'closed')
        counts = report['producer']['lifetimes']
        self.assertEqual({key: counts[key] for key in ('admitted', 'gc_completed',
            'resource_completed', 'failed_initialisations', 'failed_releases', 'current_live', 'lost')},
            {'admitted': 1, 'gc_completed': 1, 'resource_completed': 1,
             'failed_initialisations': 1, 'failed_releases': 1, 'current_live': 0, 'lost': 0})
        spans = self.lifetime_spans()
        self.assertEqual(len(spans), 2)
        boundaries = {next(a.value.string_value for a in span.attributes if a.key == 'boundary')
                      for span in spans}
        self.assertEqual(boundaries, {'observed_gc', 'resource'})
        functions = [span for request in self.capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans if span.name == 'entry.workload']
        self.assertEqual(len(functions), 1)
        for span in spans:
            self.assertFalse(span.parent_span_id)
            self.assertEqual(len(span.links), 1)
            self.assertEqual((span.links[0].trace_id, span.links[0].span_id),
                             (functions[0].trace_id, functions[0].span_id))
            self.assertGreaterEqual(span.end_time_unix_nano, span.start_time_unix_nano)

    def test_async_release_waits_for_return_and_cancellation_can_retry(self) -> None:
        source = '''import asyncio,gc,json
class Item:
    def __init__(self): pass
    async def close(self): await asyncio.sleep(0)
async def main():
    item=Item()
    task=asyncio.create_task(item.close())
    await asyncio.sleep(0)
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    await item.close()
    del task,item
asyncio.run(main());gc.collect()
print(json.dumps({'completed':True}))
'''
        result, report = self.run_app(source)
        self.assertEqual((result.returncode, report['phase']), (0, 'closed'))
        counts = report['producer']['lifetimes']
        self.assertEqual((counts['admitted'], counts['resource_completed'], counts['gc_completed'],
                          counts['failed_releases'], counts['lost']), (1, 1, 1, 1, 0))

    def test_survivors_are_censored_without_fabricating_a_gc_span(self) -> None:
        result, report = self.run_app('''class Item:
    def __init__(self): pass
    def close(self): pass
survivor=Item()
print('alive')
''')
        self.assertEqual((result.returncode, report['phase']), (0, 'closed'))
        counts = report['producer']['lifetimes']
        self.assertEqual((counts['admitted'], counts['censored'], counts['current_live'],
                          counts['open_resources'], counts['tracked_instances']), (1, 1, 1, 1, 0))
        self.assertEqual(self.lifetime_spans(), [])

    def test_lifetime_metrics_work_with_traces_disabled(self) -> None:
        self.config.write_text(self.config.read_text().replace(
            '[traces]\nenabled=true', '[traces]\nenabled=false'))
        result, report = self.run_app('''class Item:
    def __init__(self): pass
    def close(self): pass
def run():
    item=Item()
    item.close()
run();print('original')
''')
        self.assertEqual((result.returncode, report['phase']), (0, 'closed'))
        counts = report['producer']['lifetimes']
        self.assertEqual((counts['admitted'], counts['gc_completed'], counts['resource_completed'],
                          counts['lost']), (1, 1, 1, 0))
        self.assertEqual(self.lifetime_spans(), [])
        self.assertEqual(sum(point.as_int for point in self.capture.points('otelc.lifetime.completed')), 2)

    def test_release_return_type_does_not_invoke_application_metaclass_comparison(self) -> None:
        self.config.write_text(self.config.read_text().replace(
            'include=["entry.*"]', 'include=["entry.Item"]'))
        result, report = self.run_app('''import gc,json
class Meta(type):
    calls=0
    def __eq__(cls,other):
        Meta.calls+=1
        raise AssertionError('do not compare types')
class Result(metaclass=Meta): pass
class Item:
    def __init__(self): pass
    def close(self): return Result()
item=Item()
item.close()
del item
gc.collect();print(json.dumps({'hooks':Meta.calls}))
''')
        self.assertEqual((result.returncode, report['phase']), (0, 'closed'))
        counts = report['producer']['lifetimes']
        self.assertEqual((counts['admitted'], counts['resource_completed'], counts['lost']), (1, 1, 0))

    def test_generated_wrapped_inherited_initializers_keep_identity_and_raw_class_metadata(self) -> None:
        result, report = self.run_app('''import gc,json
from dataclasses import dataclass
class Meta(type):
    def __getattribute__(cls,name):
        if name in ('__dict__','__mro__','__qualname__','__module__'):
            raise AssertionError('do not dispatch class metadata')
        return super().__getattribute__(name)
def wrapper(original):
    def renamed(this,*args,**kwargs): return original(this,*args,**kwargs)
    return renamed
class Base:
    def __init__(self): pass
    def close(self): pass
class Child(Base,metaclass=Meta): pass
class Wrapped:
    @wrapper
    def __init__(self): pass
    def close(self): pass
@dataclass
class Generated:
    seed:int
    def close(self): pass
for item in (Child(),Wrapped(),Generated(7)):
    item.close()
del item
gc.collect();print(json.dumps({'completed':True}))
''')
        self.assertEqual((result.returncode, report['phase']), (0, 'closed'))
        counts = report['producer']['lifetimes']
        self.assertEqual((counts['admitted'], counts['gc_completed'], counts['resource_completed'],
                          counts['lost']), (3, 3, 3, 0))
        self.assertEqual({span.name for span in self.lifetime_spans()},
                         {'entry.Child lifetime', 'entry.Wrapped lifetime', 'entry.Generated lifetime'})

    def test_manual_initialization_is_not_a_constructor_and_custom_metaclass_loss_is_explicit(self) -> None:
        result, report = self.run_app('''import gc
class Item:
    def __init__(self): pass
manual=object.__new__(Item)
Item.__init__(manual)
del manual
class Meta(type):
    def __call__(cls):
        instance=super().__call__()
        raise ValueError('after init')
class Custom(metaclass=Meta):
    def __init__(self): pass
try: Custom()
except ValueError: pass
gc.collect();print('original')
''', loss=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['phase'], 'failed')
        counts = report['producer']['lifetimes']
        self.assertEqual((counts['admitted'], counts['gc_completed'], counts['lost']), (0, 0, 1))
        self.assertEqual(counts['losses'], {'unsupported_metaclass_call': 1})
        self.assertEqual(self.lifetime_spans(), [])

    def test_common_example_four_live_states_keep_the_same_application_and_sdk(self) -> None:
        self.entry.write_bytes((ROOT / 'examples/python/BenchmarkApp.py').read_bytes())
        digest = hashlib.sha256(self.entry.read_bytes()).hexdigest()
        self.config.write_text(self.config.read_text() + '\n[metrics]\ncollection=false\nemission=false\n')
        plain = subprocess.run([sys.executable, 'entry.py'], input='3\nquit\n',
            cwd=self.application, capture_output=True, text=True, timeout=10)
        expected = json.loads(plain.stdout)
        runtime = self.root / 'runtime'
        process = subprocess.Popen([sys.executable, '-m', 'otelc', '--config', str(self.config),
            'python', '--root', str(self.application), '--entry', 'entry.py',
            '--runtime', str(runtime)], cwd=ROOT, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            endpoint = runtime / 'control.json'
            deadline = time.monotonic() + 15
            while not endpoint.exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            first = control.request(endpoint, 'status')
            self.assertEqual((first['collection'], first['emission']), (False, False))
            identity = first['pid'], first['sdk_pid']
            for collection, emission in ((False, False), (True, False), (False, True), (True, True)):
                control.request(endpoint, 'collection ' + ('on' if collection else 'off'))
                state = control.request(endpoint, 'emission ' + ('on' if emission else 'off'))
                self.assertEqual((state['pid'], state['sdk_pid']), identity)
                self.assertEqual((state['collection'], state['emission']), (collection, emission))
                observed = batch(process, 3)
                self.assertEqual((observed['checksum'], observed['calls']), (expected['checksum'], 3))
                control.request(endpoint, 'flush')
            process.stdin.write('quit\n')
            process.stdin.flush()
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual((process.returncode, stdout, stderr), (0, '', ''))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        report = json.loads((runtime / 'sdk/python-sdk-status.json').read_text())
        self.assertEqual(report['phase'], 'closed')
        counts = report['producer']['lifetimes']
        self.assertEqual((counts['admitted'], counts['resource_completed'], counts['gc_completed'],
                          counts['lost'], counts['current_live']), (12, 12, 12, 0, 0))
        self.assertEqual(self.capture.calls(), 6)
        self.assertEqual(len(self.lifetime_spans()), 24)
        self.assertEqual(hashlib.sha256(self.entry.read_bytes()).hexdigest(), digest)

