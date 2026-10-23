"""Original CPython calls drive actual SDK data without changing app functions."""

from __future__ import annotations

import asyncio
import copy
import inspect
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otelc.config import Policy
from otelc.python_monitor import PythonMonitor
from otelc.python_runtime import PythonRuntime
from otelc.python_sdk_process import SdkProcess
from test_java_integration import Capture


SOURCE = '''import asyncio
class HostileError(Exception):
    calls = 0
    def __str__(self):
        type(self).calls += 1
        raise AssertionError('do not format me')
    def __repr__(self):
        type(self).calls += 1
        raise AssertionError('do not format me')
error = HostileError()
sentinel = object()
def workload(value: int = 1):
    if value < 0: raise error
    return sentinel
def excluded(): return 42
def recursive(depth):
    return 1 + recursive(depth - 1) if depth else 1
async def child(value):
    await asyncio.sleep(.001)
    return value
async def outer():
    return await asyncio.gather(child(1), child(2))
async def cancelled():
    await asyncio.sleep(60)
def generator():
    yield sentinel
    return 42
'''


class PythonMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'fixture.py'
        self.source.write_text(SOURCE)
        self.namespace = {}
        exec(compile(SOURCE, str(self.source), 'exec'), self.namespace)
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.policy = Policy.parse({'schema_version': 3,
            'sources': {'include': ['*.py']},
            'functions': {'include': ['fixture.*'], 'exclude': ['*excluded', '*HostileError*']},
            'metrics': {'collection': False, 'emission': False},
            'traces': {'enabled': True, 'sample_ratio': 1.0},
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 100000, 'timeout_ms': 1000}})
        self.sdk = SdkProcess.start(self.policy, self.root / 'runtime')
        self.addCleanup(self.sdk.abort)
        self.runtime = PythonRuntime(self.policy, self.sdk.channel)
        self.monitor = PythonMonitor(self.policy, self.root, self.runtime)
        self.addCleanup(self.monitor.close)

    def finish(self):
        self.monitor.close()
        self.runtime.close()
        self.runtime.close()
        return self.sdk.wait_report()

    def spans(self):
        return [span for request in self.capture.traces for resource in request.resource_spans
                for scope in resource.scope_spans for span in scope.spans]

    def test_original_calls_exceptions_rng_and_four_same_worker_states(self) -> None:
        workload = self.namespace['workload']
        original_code = workload.__code__
        signature = inspect.signature(workload)
        before = random.getstate()
        self.monitor.install()
        for collection, emission in ((False, False), (True, False),
                                     (False, True), (True, True)):
            response = self.runtime.controls(collection, emission)
            self.assertEqual((response['collection'], response['emission']), (collection, emission))
            self.assertIs(workload(), self.namespace['sentinel'])
            self.assertEqual(self.namespace['excluded'](), 42)
            self.sdk.channel.request('flush')
        with self.assertRaises(self.namespace['HostileError']) as raised:
            workload(-1)
        self.assertIs(raised.exception, self.namespace['error'])
        self.assertEqual(self.namespace['HostileError'].calls, 0)
        self.assertEqual(random.getstate(), before)
        self.assertIs(workload.__code__, original_code)
        self.assertEqual(inspect.signature(workload), signature)
        report = self.finish()
        self.assertEqual(report['phase'], 'closed')
        self.assertEqual(report['producer']['function_completions'], 5)
        self.assertEqual(self.capture.calls(), 3)
        spans = self.spans()
        self.assertEqual(len(spans), 5)
        errors = [span for span in spans if span.status.code == 2]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].events[0].attributes[0].key, 'exception.type')
        self.assertEqual(errors[0].events[0].attributes[0].value.string_value, 'HostileError')
        self.assertEqual(self.source.read_text(), SOURCE)

    def test_async_suspension_task_parentage_cancellation_and_generator_completion(self) -> None:
        self.monitor.install()
        self.assertEqual(asyncio.run(self.namespace['outer']()), [1, 2])
        generator = self.namespace['generator']()
        self.assertIs(next(generator), self.namespace['sentinel'])
        self.assertEqual(self.runtime.status()['active_functions'], 1)
        with self.assertRaises(StopIteration) as ended:
            next(generator)
        self.assertEqual(ended.exception.value, 42)

        async def cancel() -> None:
            task = asyncio.create_task(self.namespace['cancelled']())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(cancel())
        report = self.finish()
        self.assertEqual(report['phase'], 'closed')
        self.assertEqual(report['producer']['active_functions'], 0)
        spans = self.spans()
        self.assertEqual(len(spans), 5)
        outer = next(span for span in spans if span.name == 'fixture.outer')
        children = [span for span in spans if span.name == 'fixture.child']
        self.assertEqual(len(children), 2)
        for child in children:
            self.assertEqual(child.trace_id, outer.trace_id)
            self.assertEqual(child.parent_span_id, outer.span_id)
        cancelled = next(span for span in spans if span.name == 'fixture.cancelled')
        self.assertEqual(cancelled.status.code, 2)
        self.assertEqual(cancelled.events[0].attributes[0].value.string_value, 'CancelledError')

    def test_partial_or_occupied_monitor_installation_keeps_other_tools(self) -> None:
        monitoring = sys.monitoring
        monitoring.use_tool_id(3, 'fixture-owner')
        self.addCleanup(monitoring.free_tool_id, 3)
        self.monitor.install()
        self.assertNotEqual(self.monitor.tool, 3)
        tool = self.monitor.tool
        with self.assertRaises(RuntimeError):
            self.monitor.install()
        self.monitor.close()
        self.assertIsNone(monitoring.get_tool(tool))
        self.assertEqual(monitoring.get_events(tool), 0)
        self.assertEqual(monitoring.get_tool(3), 'fixture-owner')
        original = monitoring.register_callback
        calls = 0

        def controlled(identity, event, callback):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError('controlled installation failure')
            return original(identity, event, callback)

        with mock.patch.object(monitoring, 'register_callback', side_effect=controlled):
            with self.assertRaisesRegex(ValueError, 'controlled'):
                self.monitor.install()
        self.assertIsNone(self.monitor.tool)
        self.assertIsNone(monitoring.get_tool(tool))
        for identity in (4, 2, 5):
            monitoring.use_tool_id(identity, 'fixture-owner')
            self.addCleanup(monitoring.free_tool_id, identity)
        with self.assertRaisesRegex(ValueError, 'no free'):
            self.monitor.install()
        self.finish()

    def test_probe_failure_preserves_original_values_and_fails_final_evidence(self) -> None:
        self.monitor.install()
        with mock.patch.object(self.runtime, 'enter', side_effect=ValueError('controlled')):
            self.assertIs(self.namespace['workload'](), self.namespace['sentinel'])
        report = self.finish()
        self.assertEqual(report['phase'], 'failed')
        self.assertEqual(report['producer']['runtime_losses']['monitor_start'], 1)

    def test_censored_suspended_call_is_not_completed_by_shutdown(self) -> None:
        self.monitor.install()
        generator = self.namespace['generator']()
        self.assertIs(next(generator), self.namespace['sentinel'])
        report = self.finish()
        self.assertEqual(report['phase'], 'failed')
        self.assertEqual(report['producer']['active_functions'], 1)
        self.assertEqual(report['producer']['function_completions'], 0)
        self.assertEqual(len(self.spans()), 0)
        generator.close()

    def test_source_exclusions_producer_protection_and_code_identity(self) -> None:
        code = self.namespace['workload'].__code__
        self.assertEqual(self.monitor.selected(code), 'fixture.workload')
        self.assertIs(self.monitor._names[id(code)][0](), code)
        self.assertIsNone(self.monitor.selected(compile('42', str(self.source), 'eval')))
        self.assertIsNone(self.monitor.selected(code.replace(co_filename='relative.py')))
        self.assertIsNone(self.monitor.selected(PythonMonitor.close.__code__))
        data = copy.deepcopy(self.policy.data)
        data['sources']['exclude'] = ['*.py']
        excluded = PythonMonitor(Policy.parse(data), self.root, self.runtime)
        self.assertIsNone(excluded.selected(code))
        data['sources']['exclude'] = []
        data['functions']['exclude'] = ['*workload']
        excluded = PythonMonitor(Policy.parse(data), self.root, self.runtime)
        self.assertIsNone(excluded.selected(code))
        self.finish()

    def test_recursive_original_calls_keep_exact_sdk_parent_chain(self) -> None:
        self.runtime.controls(True, True)
        self.monitor.install()
        self.assertEqual(self.namespace['recursive'](2), 3)
        self.assertEqual(self.finish()['phase'], 'closed')
        spans = self.spans()
        self.assertEqual(len(spans), 3)
        self.assertEqual(len({span.span_id for span in spans}), 3)
        self.assertEqual(len({span.trace_id for span in spans}), 1)
        roots = [span for span in spans if not span.parent_span_id]
        self.assertEqual(len(roots), 1)
        root = roots[0]
        child = next(span for span in spans if span.parent_span_id == root.span_id)
        self.assertEqual(len([span for span in spans if span.parent_span_id == child.span_id]), 1)
        self.assertEqual(self.capture.calls(), 3)
