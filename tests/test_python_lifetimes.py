"""Qualify collection without retaining instances or extending function trees."""
import gc
import unittest
import weakref
from test_python_spans import PythonSpanHarness, trace_plan


def collection_plan():
    plan = trace_plan()
    plan['lifetimes'] = {'enabled': True, 'boundary': 'collection'}
    plan['lifetime_matchers'] = {'include': ['(?-u)^trace_fixture\\..*$'], 'exclude': []}
    plan['runtime']['max_live_lifetimes'] = 8
    return plan


class PythonLifetimeTests(PythonSpanHarness, unittest.TestCase):
    def test_successful_initialisation_collects_after_creator_without_retention(self):
        runtime, _, capture, app, monitor = self.monitored('''
class Thing:
    def __init__(self, value):
        self.value = value

def create():
    return Thing(7)
''', collection_plan())
        value = app['create']()
        self.assertEqual(value.value, 7)
        reference = weakref.ref(value)
        self.assertEqual(runtime.lifetimes.report()['current_live'], 1)
        self.assertFalse(runtime.traces.roots)
        del value
        self.assertIsNone(reference())
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['admitted'], 1)
        self.assertEqual(report['lifetimes']['completed'], 1)
        lifetime = next(span for span in capture.spans() if span.name == 'trace_fixture.Thing lifetime')
        self.assertFalse(lifetime.parent_span_id)
        self.assertEqual(len(lifetime.links), 1)
        constructor = next(span for span in capture.spans() if span.name == 'trace_fixture.Thing.__init__')
        self.assertEqual(lifetime.links[0].span_id, constructor.span_id)
        self.assertNotEqual(lifetime.trace_id, constructor.trace_id)
        self.assertGreaterEqual(lifetime.end_time_unix_nano, lifetime.start_time_unix_nano)
        self.assertFalse(report['lifetimes']['losses'])

    def test_constructor_chains_failures_reinitialisation_and_hostile_hash(self):
        runtime, _, capture, app, monitor = self.monitored('''
class Base:
    def __init__(self):
        self.value = 3
class Child(Base):
    def __init__(self):
        super().__init__()
    def __hash__(self):
        raise AssertionError('observer hashed object')
    def __eq__(self, other):
        raise AssertionError('observer compared object')
class Failed:
    def __init__(self):
        raise failure
failure = ValueError('original')
class BadReturn:
    def __init__(self):
        return 4
''', collection_plan())
        obj = app['Child']()
        obj.__init__()
        self.assertEqual(obj.value, 3)
        self.assertEqual(runtime.lifetimes.admitted, 1)
        with self.assertRaisesRegex(ValueError, 'original') as caught:
            app['Failed']()
        self.assertIs(caught.exception, app['failure'])
        with self.assertRaises(TypeError):
            app['BadReturn']()
        del obj
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 1)
        self.assertEqual(report['lifetimes']['failed_initialisations'], 2)
        self.assertFalse(report['lifetimes']['losses'])
        self.assertEqual(len([s for s in capture.spans() if s.name.endswith(' lifetime')]), 1)

    def test_cycles_are_collectible_and_unclosed_instances_are_censored(self):
        runtime, _, capture, app, monitor = self.monitored('''
class Cycle:
    def __init__(self):
        self.cycle = self
class Open:
    def __init__(self):
        pass
''', collection_plan())
        obj = app['Cycle']()
        reference = weakref.ref(obj)
        del obj
        gc.collect()  # Only the test forces GC; the adapter must never do so.
        self.assertIsNone(reference())
        still_live = app['Open']()
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 1)
        self.assertEqual(report['lifetimes']['censored'], 1)
        self.assertEqual(report['lifetimes']['current_live'], 1)
        self.assertEqual(report['lifetimes']['tracked_instances'], 0)
        self.assertEqual(report['lifetimes']['losses']['incomplete'], 1)
        self.assertEqual(len([s for s in capture.spans() if s.name.endswith(' lifetime')]), 1)
        del still_live
        self.assertEqual(runtime.lifetimes.completed, 1)

    def test_selection_is_independent_of_functions_and_sampling_metrics_controls(self):
        policy = collection_plan()
        policy['function_matchers'] = {'include': [], 'exclude': []}
        policy['lifetime_matchers']['exclude'] = ['(?-u)^.*Excluded$']
        policy['traces']['root_sample_ratio'] = 0
        runtime, metrics, capture, app, monitor = self.monitored('''
class Thing:
    def __init__(self):
        pass
class Excluded:
    def __init__(self):
        pass
''', policy)
        obj = app['Thing']()
        runtime.enabled = False
        del obj
        disabled = app['Thing']()
        runtime.enabled = True
        del disabled
        excluded = app['Excluded']()
        del excluded
        self.assertEqual(runtime.lifetimes.admitted, 2)
        self.assertEqual(runtime.lifetimes.completed, 2)
        self.assertEqual(runtime.lifetimes.points('admitted')[0].value, 1)
        self.assertEqual(runtime.lifetimes.points('completed')[0].value, 1)
        self.assertEqual(runtime.lifetimes.points('live')[0].value, 0)
        report = self.finish(runtime, monitor)
        self.assertFalse(capture.spans())
        self.assertEqual(report['function_calls'], 0)
        self.assertFalse(report['lifetimes']['losses'])
        names = {m.name for b in metrics.batches for r in b.resource_metrics for s in r.scope_metrics for m in s.metrics}
        self.assertIn('otelc.lifetime.duration', names)
        self.assertIn('otelc.lifetime.live', names)

    def test_supported_slots_and_unsupported_layout_finalizer_constructor_metaclass(self):
        policy = collection_plan()
        policy['function_matchers'] = {'include': [], 'exclude': []}
        runtime, _, _, app, monitor = self.monitored('''
class Slots:
    __slots__ = ('__weakref__',)
    def __init__(self):
        pass
class NoWeak:
    __slots__ = ()
    def __init__(self):
        pass
class Finalizer:
    def __init__(self):
        pass
    def __del__(self):
        pass
class CustomNew:
    def __new__(cls):
        return object.__new__(cls)
    def __init__(self):
        pass
class Meta(type):
    pass
class CustomMeta(metaclass=Meta):
    def __init__(self):
        pass
class Parent:
    def __init__(self):
        pass
class Inherited(Parent):
    pass
''', policy)
        for name in ('Slots', 'NoWeak', 'Finalizer', 'CustomNew', 'CustomMeta', 'Inherited'):
            obj = app[name]()
            del obj
        report = self.finish(runtime, monitor)['lifetimes']
        self.assertEqual(report['admitted'], 1)
        self.assertEqual(report['completed'], 1)
        for reason in ('unsupported_weak_reference', 'unsupported_finalizer', 'unsupported_constructor', 'unsupported_metaclass', 'unsupported_initializer'):
            self.assertEqual(report['losses'][reason], 1)

    def test_capacity_failures_do_not_keep_receivers_and_tokens_are_unique(self):
        policy = collection_plan()
        policy['function_matchers'] = {'include': [], 'exclude': []}
        policy['runtime']['max_live_lifetimes'] = 1
        runtime, _, capture, app, monitor = self.monitored('''
class Thing:
    def __init__(self):
        pass
''', policy)
        first = app['Thing']()
        rejected = app['Thing']()
        reference = weakref.ref(rejected)
        del rejected
        self.assertIsNone(reference())
        token1 = next(iter(runtime.lifetimes.instances))
        del first
        next_obj = app['Thing']()
        token2 = next(iter(runtime.lifetimes.instances))
        self.assertNotEqual(token1, token2)
        del next_obj
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 2)
        self.assertEqual(report['lifetimes']['losses']['lifetime_capacity'], 1)
        self.assertEqual(len([s for s in capture.spans() if s.name.endswith(' lifetime')]), 2)

    def test_registry_boundary_failures_capacity_and_shutdown_do_not_retain_frames(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from test_python_adapter import ROOT
        runtime, _, capture, app, _ = self.monitored('''
class Thing:
    def __init__(self):
        pass
class Other:
    def __init__(self):
        pass
class Rebound:
    def __init__(self):
        pass
''', collection_plan())
        # Direct registry calls make hook handling observable to coverage; the
        # real monitoring tests above separately qualify CPython event/frame use.
        runtime.monitor.close()
        owner = runtime.lifetimes
        thing = app['Thing']()
        code = app['Thing'].__init__.__code__
        frame = SimpleNamespace(f_locals={'self': thing})
        owner.start(frame, code, ROOT)
        owner.start(frame, code, ROOT)
        self.assertEqual(len(owner.frames), 1)
        runtime.plan['runtime']['max_active_calls'] = 1
        other = app['Other']()
        other_frame = SimpleNamespace(f_locals={'self': other})
        other_code = app['Other'].__init__.__code__
        owner.start(other_frame, other_code, ROOT)
        self.assertEqual(owner.losses['constructor_capacity'], 1)
        owner.returned(frame, code, None, None)
        self.assertEqual(owner.admitted, 1)
        owner.start(frame, code, ROOT)
        self.assertFalse(owner.frames)
        token = next(iter(owner.instances))
        frame.f_locals.clear()
        reference = weakref.ref(thing)
        with patch.object(owner.duration, 'record', side_effect=ValueError('metric failed')):
            del thing
        self.assertIsNone(reference())
        self.assertEqual(owner.completed, 1)
        self.assertEqual(owner.losses['invalid'], 1)
        owner.collected(token)
        self.assertEqual(owner.losses['stale_completion'], 1)
        runtime.plan['runtime']['max_functions'] = 1
        owner.start(other_frame, other_code, ROOT)
        self.assertEqual(owner.losses['type_capacity'], 1)
        runtime.plan['runtime']['max_functions'] = 8
        owner.start(other_frame, other_code, ROOT)
        with patch.object(runtime.traces, 'begin_lifetime', side_effect=ValueError('span failed')):
            owner.returned(other_frame, other_code, None, None)
        self.assertEqual(owner.losses['invalid'], 2)
        owner.start(other_frame, other_code, ROOT)
        other_frame.f_locals['self'] = object()
        owner.returned(other_frame, other_code, None, None)
        self.assertEqual(owner.losses['receiver_changed'], 1)
        other_frame.f_locals['self'] = other
        owner.start(other_frame, other_code, ROOT)
        owner.shutdown()
        self.assertEqual(owner.losses['incomplete_constructor'], 1)
        self.assertFalse(owner.frames)
        self.assertFalse(owner.initialising)
        owner.collected(99999)
        owner.start(other_frame, other_code, ROOT)
        self.assertFalse(owner.frames)
        runtime.close()
        self.assertEqual(len([s for s in capture.spans() if s.name.endswith(' lifetime')]), 1)

    def test_direct_registry_qualification_and_failed_callbacks_are_visible(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from test_python_adapter import ROOT
        runtime, _, _, app, monitor = self.monitored('''
class Thing:
    def __init__(self):
        pass
    def other(self):
        return None
class NoWeak:
    __slots__ = ()
    def __init__(self):
        pass
class Finalizer:
    def __init__(self):
        pass
    def __del__(self):
        pass
class CustomNew:
    def __new__(cls):
        return object.__new__(cls)
    def __init__(self):
        pass
class Parent:
    def __init__(self):
        pass
class Child(Parent):
    def __init__(self):
        super().__init__()
class Inherited(Parent):
    pass
class Meta(type):
    pass
class CustomMeta(metaclass=Meta):
    def __init__(self):
        pass
''', collection_plan())
        monitor.close()
        owner = runtime.lifetimes
        for name in ('Thing', 'NoWeak', 'Finalizer', 'CustomNew', 'Inherited', 'CustomMeta'):
            obj = app[name]()
            code = app[name].__init__.__code__
            frame = SimpleNamespace(f_locals={'self': obj})
            owner.start(frame, code, ROOT)
            if name == 'Thing':
                owner.unwound(frame)
                owner.unwound(frame)
                owner.start(frame, code, ROOT)
                owner.returned(frame, code, 2, None)
                owner.start(frame, code, ROOT)
                with patch.object(runtime.traces, 'finish_lifetime', side_effect=ValueError('span failed')):
                    owner.returned(frame, code, None, None)
                    frame.f_locals.clear()
                    del obj
        self.assertEqual(owner.failed_initialisations, 2)
        self.assertEqual(owner.losses['invalid'], 1)
        for reason in ('unsupported_weak_reference', 'unsupported_finalizer', 'unsupported_constructor', 'unsupported_metaclass', 'unsupported_initializer'):
            self.assertEqual(owner.losses[reason], 1)
        obj = app['Child']()
        frame = SimpleNamespace(f_locals={'self': obj})
        owner.start(frame, app['Parent'].__init__.__code__, ROOT)
        self.assertFalse(owner.frames)
        owner.start(frame, app['Thing'].other.__code__, ROOT)
        owner.start(frame, app['Child'].__init__.__code__, ROOT / 'outside')
        self.assertFalse(owner.frames)
        owner.unwound(frame)
        owner.selection.include.clear()
        owner.start(frame, app['Child'].__init__.__code__, ROOT)
        self.assertFalse(owner.frames)
        self.assertTrue(owner.loss_points(None))
        runtime.close()

    def test_lifetime_queue_capacity_and_bad_resolved_settings_fail_visibly(self):
        from test_python_adapter import Capture
        from test_python_spans import TraceCapture
        from quux_otelc_python.telemetry import Runtime
        for boundary, tracing, maximum in [('object', True, 1), ('collection', False, 1), ('collection', True, 0)]:
            policy = collection_plan()
            policy['lifetimes']['boundary'] = boundary
            policy['traces']['enabled'] = tracing
            policy['runtime']['max_live_lifetimes'] = maximum
            with self.assertRaisesRegex(ValueError, 'collection lifetimes require'):
                Runtime(policy, Capture(), TraceCapture())
        policy = collection_plan()
        policy['function_matchers'] = {'include': [], 'exclude': []}
        policy['export']['max_queued_batches'] = 1
        runtime, _, capture, app, monitor = self.monitored('''
class Thing:
    def __init__(self):
        pass
''', policy)
        for _ in range(2):
            obj = app['Thing']()
            del obj
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 2)
        self.assertEqual(report['traces']['losses']['queue_capacity'], 1)
        self.assertEqual(len(capture.spans()), 1)

    def test_nested_initialisers_cannot_overbook_type_identities(self):
        policy = collection_plan()
        policy['function_matchers'] = {'include': [], 'exclude': []}
        policy['runtime']['max_functions'] = 1
        runtime, _, capture, app, monitor = self.monitored('''
class Inner:
    def __init__(self):
        pass
class Outer:
    def __init__(self):
        self.inner = Inner()
''', policy)
        value = app['Outer']()
        self.assertEqual(type(value.inner).__name__, 'Inner')
        self.assertEqual(len(runtime.lifetimes.types), 1)
        self.assertEqual(runtime.lifetimes.losses['type_capacity'], 1)
        del value
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 1)
        self.assertEqual(len(capture.spans()), 1)

    def test_cross_thread_collection_has_owner_cleanup_without_payload_retention(self):
        import threading
        policy = collection_plan()
        policy['function_matchers'] = {'include': [], 'exclude': []}
        runtime, _, capture, app, monitor = self.monitored('''
class Thing:
    def __init__(self):
        pass
''', policy)
        references = []
        gate = threading.Barrier(5)
        def worker():
            value = app['Thing']()
            references.append(weakref.ref(value))
            gate.wait(timeout=5)
        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(references), 5)
        self.assertTrue(all(reference() is None for reference in references))
        report = self.finish(runtime, monitor)
        self.assertEqual(report['lifetimes']['completed'], 5)
        self.assertEqual(report['lifetimes']['current_live'], 0)
        self.assertFalse(report['lifetimes']['losses'])
        self.assertEqual(len(capture.spans()), 5)

    def test_nonordinary_initializer_shapes_are_not_admitted(self):
        from types import SimpleNamespace
        from test_python_adapter import ROOT
        runtime, _, capture, app, monitor = self.monitored('''
class Async:
    async def __init__(self):
        pass
class Generator:
    def __init__(self):
        yield None
class Arguments:
    def __init__(*args):
        pass
''', collection_plan())
        monitor.close()
        for name in ('Async', 'Generator', 'Arguments'):
            obj = object.__new__(app[name])
            frame = SimpleNamespace(f_locals={'self': obj, 'args': (obj,)})
            runtime.lifetimes.start(frame, app[name].__init__.__code__, ROOT)
        report = runtime.close()
        self.assertEqual(report['lifetimes']['admitted'], 0)
        self.assertEqual(report['lifetimes']['losses']['unsupported_initializer'], 3)
        self.assertFalse(capture.spans())
