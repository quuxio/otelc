"""Qualify explicitly bound existing release methods without rewriting classes."""
import gc
import threading
import unittest
import weakref
from unittest.mock import patch
from types import SimpleNamespace

from test_python_lifetimes import collection_plan
from test_python_spans import PythonSpanHarness
from test_python_adapter import Capture
from test_python_adapter import ROOT
from quux_otelc_python.telemetry import Runtime


def resource_plan():
    policy = collection_plan()
    policy['lifetimes'].update(boundary='resource', resources=[
        {'type_name': 'trace_fixture.Resource', 'release': 'close', 'completion': 'normal_return'}])
    return policy


SOURCE = '''
failure = ValueError('original')
class Resource:
    def __init__(self, value=7):
        self.value = value
        self.closed = False
    def close(self, fail=False):
        if fail:
            raise failure
        self.closed = True
        return self.value
    def __hash__(self):
        raise AssertionError('observer called hash')
    def __eq__(self, other):
        raise AssertionError('observer called equality')
'''


class PythonResourceTests(PythonSpanHarness, unittest.TestCase):
    def test_hook_protocol_handles_rebound_receivers_stale_returns_and_transport_failure(self):
        # CPython suspends tracing inside monitoring callbacks. Exercise the
        # hook protocol directly as well as the actual-event tests below.
        runtime, _, capture, app, monitor = self.monitored(SOURCE, resource_plan())
        monitor.close()
        owner = runtime.lifetimes
        cls = app['Resource']

        def admit(value):
            frame = SimpleNamespace(f_locals={'self': value})
            owner.start(frame, cls.__init__.__code__, ROOT)
            owner.returned(frame, cls.__init__.__code__, None, None)

        first, second = cls(), cls()
        self.assertTrue(owner.releases.qualified('trace_fixture.Resource', vars(cls)))
        self.assertFalse(owner.releases.qualified('trace_fixture.Resource', {}))
        admit(first); admit(second)
        frame = SimpleNamespace(f_locals={'self': first}, f_back=None)
        owner.start(frame, cls.close.__code__, ROOT)
        owner.unwound(frame)
        self.assertEqual(owner.releases.failed, 1)
        owner.start(frame, cls.close.__code__, ROOT)
        frame.f_locals['self'] = second
        owner.returned(frame, cls.close.__code__, 7, None)
        self.assertEqual(owner.completed, 0)
        self.assertEqual(owner.losses['receiver_changed'], 1)
        frame.f_locals['self'] = first
        owner.start(frame, cls.close.__code__, ROOT)
        nested = SimpleNamespace(f_locals={'self': first}, f_back=frame)
        owner.start(nested, cls.close.__code__, ROOT)
        owner.returned(nested, cls.close.__code__, 7, None)
        self.assertEqual(owner.completed, 0)
        concurrent = SimpleNamespace(f_locals={'self': first}, f_back=None)
        owner.start(concurrent, cls.close.__code__, ROOT)
        owner.returned(frame, cls.close.__code__, 7, None)
        owner.returned(concurrent, cls.close.__code__, 7, None)
        self.assertEqual(owner.completed, 1)
        owner.start(frame, cls.close.__code__, ROOT)
        self.assertFalse(owner.releases.frames)
        runtime.plan['runtime']['max_active_calls'] = 1
        frame.f_locals['self'] = second
        owner.start(frame, cls.close.__code__, ROOT)
        other = SimpleNamespace(f_locals={'self': second}, f_back=None)
        owner.start(other, cls.close.__code__, ROOT)
        self.assertEqual(owner.losses['release_capacity'], 1)
        with patch.object(runtime.traces, 'finish_lifetime', side_effect=RuntimeError('transport')):
            owner.returned(frame, cls.close.__code__, 7, None)
        self.assertEqual(owner.completed, 2)
        self.assertEqual(owner.losses['invalid'], 1)
        runtime.plan['runtime']['max_active_calls'] = 2
        third, fourth = cls(), cls()
        admit(third); admit(fourth)
        third_frame = SimpleNamespace(f_locals={'self': third}, f_back=None)
        fourth_frame = SimpleNamespace(f_locals={'self': fourth}, f_back=None)
        owner.start(third_frame, cls.close.__code__, ROOT)
        owner.start(fourth_frame, cls.close.__code__, ROOT)
        report = runtime.close()
        self.assertEqual(report['lifetimes']['active_releases'], 0)
        self.assertEqual(report['lifetimes']['losses']['incomplete_release'], 2)

    def test_invalid_resolved_bindings_fail_before_sdk_or_application_setup(self):
        invalid = [None, [], [{}], ['close'],
                   [{'type_name': 'trace_fixture.*', 'release': 'close'}],
                   [{'type_name': 'trace_fixture.Resource', 'release': '__init__'}],
                   [{'type_name': 'trace_fixture.Resource', 'release': 'close', 'completion': 'truthy'}],
                   [{'type_name': 'trace_fixture.Resource', 'release': 'close', 'unknown': True}],
                   [{'type_name': 'trace_fixture.Resource', 'release': 'close'}] * 2,
                   [{'type_name': 'trace_fixture.Resource', 'release': 'close'}] * 129]
        for entries in invalid:
            policy = resource_plan()
            policy['lifetimes']['resources'] = entries
            with self.subTest(entries=entries), patch('quux_otelc_python.telemetry.MeterProvider') as sdk:
                with self.assertRaises(ValueError):
                    Runtime(policy, Capture())
                sdk.assert_not_called()
        policy = collection_plan()
        policy['lifetimes']['resources'] = resource_plan()['lifetimes']['resources']
        with self.assertRaisesRegex(ValueError, 'resource boundary'):
            Runtime(policy, Capture())

    def test_successful_close_completes_before_collection_with_creation_link(self):
        runtime, _, capture, app, monitor = self.monitored(SOURCE, resource_plan())
        value = app['Resource']()
        reference = weakref.ref(value)
        self.assertEqual(value.__dict__, {'value': 7, 'closed': False})
        self.assertEqual(runtime.lifetimes.completed, 0)
        self.assertEqual(value.close(), 7)
        self.assertEqual(value.close(), 7)
        self.assertTrue(value.closed)
        self.assertEqual(runtime.lifetimes.completed, 1)
        self.assertIs(reference(), value)
        report = self.finish(runtime, monitor)
        lifetimes = [span for span in capture.spans() if span.name.endswith(' lifetime')]
        self.assertEqual(len(lifetimes), 1)
        attrs = {item.key: item.value.string_value for item in lifetimes[0].attributes}
        self.assertEqual(attrs['otelc.lifetime.boundary'], 'resource')
        self.assertEqual(len(lifetimes[0].links), 1)
        self.assertFalse(lifetimes[0].parent_span_id)
        self.assertEqual(report['lifetimes']['current_live'], 0)
        self.assertFalse(report['lifetimes']['losses'])
        self.assertEqual(runtime.lifetimes.points('completed')[0].attributes['otelc.lifetime.boundary'], 'resource')

    def test_failed_release_preserves_exception_and_allows_later_success(self):
        runtime, _, capture, app, monitor = self.monitored(SOURCE, resource_plan())
        value = app['Resource']()
        with self.assertRaises(ValueError) as caught:
            value.close(fail=True)
        self.assertIs(caught.exception, app['failure'])
        self.assertFalse(value.closed)
        self.assertEqual(runtime.lifetimes.completed, 0)
        self.assertEqual(runtime.lifetimes.report()['failed_releases'], 1)
        self.assertEqual(value.close(), 7)
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 1)
        self.assertFalse(report['lifetimes']['losses'])

    def test_closed_instances_collect_while_the_observer_remains_active(self):
        runtime, _, _, app, monitor = self.monitored(SOURCE, resource_plan())
        value = app['Resource']()
        reference = weakref.ref(value)
        value.close()
        control = [value]
        del value
        gc.collect()
        self.assertIs(reference(), control[0], 'the retention control must stay alive')
        control.clear()
        gc.collect()
        self.assertIsNone(reference(), 'the running observer must not retain a closed instance')
        self.assertFalse(runtime.closed)
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 1)
        self.assertEqual(report['lifetimes']['censored'], 0)
        self.assertFalse(report['lifetimes']['losses'])

    def test_collection_without_release_is_censored_without_completed_span(self):
        runtime, _, capture, app, monitor = self.monitored(SOURCE, resource_plan())
        value = app['Resource']()
        reference = weakref.ref(value)
        del value
        gc.collect()
        self.assertIsNone(reference())
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 0)
        self.assertEqual(report['lifetimes']['censored'], 1)
        self.assertEqual(report['lifetimes']['current_live'], 1)
        self.assertEqual(report['lifetimes']['losses']['unreleased_collection'], 1)
        self.assertFalse([span for span in capture.spans() if span.name.endswith(' lifetime')])

    def test_recursive_release_waits_for_outer_return_and_concurrent_success_completes_once(self):
        runtime, _, _, app, monitor = self.monitored('''
class Resource:
    def __init__(self): pass
    def close(self, recursive=False, fail=False, entered=None, resume=None):
        if recursive:
            self.close()
        if entered is not None:
            entered.set()
            resume.wait(2)
        if fail: raise ValueError('failed outer release')
''', resource_plan())
        value = app['Resource']()
        with self.assertRaises(ValueError):
            value.close(recursive=True, fail=True)
        self.assertEqual(runtime.lifetimes.completed, 0)
        entered, resume = threading.Event(), threading.Event()
        worker = threading.Thread(target=value.close, kwargs={'entered': entered, 'resume': resume})
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            value.close()
            self.assertEqual(runtime.lifetimes.completed, 1)
        finally:
            resume.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 1)
        self.assertFalse(report['lifetimes']['active_releases'])
        self.assertFalse(report['lifetimes']['losses'])

    def test_unbound_classes_and_unsupported_release_shapes_do_not_admit(self):
        variants = [
            '    async def close(self): pass',
            '    def close(self): yield 1',
            '    @property\n    def close(self): raise AssertionError("observer evaluated descriptor")',
            '    @staticmethod\n    def close(): pass',
            '    @classmethod\n    def close(cls): pass',
            '    close = len',
            '    def different(self): pass',
        ]
        for release in variants:
            with self.subTest(release=release):
                runtime, _, _, app, monitor = self.monitored(
                    'class Resource:\n    def __init__(self): pass\n' + release + '\n', resource_plan())
                value = app['Resource']()
                self.assertEqual(runtime.lifetimes.admitted, 0)
                self.assertEqual(runtime.lifetimes.losses['unsupported_release'], 1)
                self.finish(runtime, monitor)
        runtime, _, _, app, monitor = self.monitored(SOURCE.replace('class Resource:', 'class Unbound:'), resource_plan())
        value = app['Unbound']()
        value.close()
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['admitted'], 0)
        self.assertFalse(report['lifetimes']['losses'])

    def test_release_capacity_is_visible_and_inflight_shutdown_is_incomplete(self):
        policy = resource_plan()
        policy['runtime']['max_active_calls'] = 1
        policy['function_matchers'] = {'include': [], 'exclude': []}
        runtime, _, _, app, monitor = self.monitored('''
class Resource:
    def __init__(self): pass
    def close(self, entered=None, resume=None):
        if entered is not None:
            entered.set()
            resume.wait(2)
''', policy)
        first, second = app['Resource'](), app['Resource']()
        entered, resume = threading.Event(), threading.Event()
        worker = threading.Thread(target=first.close, kwargs={'entered': entered, 'resume': resume})
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            second.close()
            self.assertEqual(runtime.lifetimes.losses['release_capacity'], 1)
            report = self.finish(runtime, monitor)
            self.assertEqual(report['lifetimes']['censored'], 2)
            self.assertEqual(report['lifetimes']['completed'], 0)
            self.assertEqual(report['lifetimes']['losses']['incomplete_release'], 1)
            self.assertEqual(report['lifetimes']['active_releases'], 0)
        finally:
            resume.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_false_return_is_success_and_telemetry_failure_cannot_change_it(self):
        runtime, _, _, app, monitor = self.monitored(SOURCE.replace('return self.value', 'return False'), resource_plan())
        value = app['Resource']()
        with patch.object(runtime.traces, 'finish_lifetime', side_effect=ValueError('SDK failure')):
            self.assertIs(value.close(), False)
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 1)
        self.assertEqual(report['lifetimes']['losses']['invalid'], 1)

    def test_sampling_and_metrics_controls_preserve_resource_admission(self):
        policy = resource_plan()
        policy['function_matchers'] = {'include': [], 'exclude': []}
        policy['traces']['root_sample_ratio'] = 0
        runtime, _, capture, app, monitor = self.monitored(SOURCE, policy)
        first = app['Resource']()
        runtime.enabled = False
        second = app['Resource']()
        self.assertEqual(first.close(), 7)
        runtime.enabled = True
        self.assertEqual(second.close(), 7)
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 2)
        self.assertEqual(runtime.lifetimes.points('admitted')[0].value, 1)
        self.assertEqual(runtime.lifetimes.points('completed')[0].value, 1)
        self.assertEqual(runtime.lifetimes.points('live')[0].value, 0)
        self.assertFalse(capture.spans())
        self.assertFalse(report['lifetimes']['losses'])
