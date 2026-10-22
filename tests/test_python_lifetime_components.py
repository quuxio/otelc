"""Direct boundary cases complement native callbacks which suppress trace coverage."""

from __future__ import annotations

import gc
import threading
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from otelc.config import Policy
from otelc.python_lifetimes import Lifetimes
from otelc import python_objects as objects


class Item:
    def __init__(self):
        pass

    def close(self):
        pass


class UnreferencedItem:
    __slots__ = ()

    def __init__(self):
        pass


class DefaultItem:
    pass


class Runtime:
    def __init__(self):
        self.collection = True
        self._lock = threading.RLock()
        self._provider = None
        self.metrics = []

    def _metric(self, *args, **kwargs):
        self.metrics.append((args, kwargs))


class PythonLifetimeComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy.parse({'schema_version': 3,
            'sources': {'include': ['test_python_lifetime_components.py']},
            'lifetimes': {'enabled': True, 'include': ['*']}})
        self.runtime = Runtime()
        self.tracker = Lifetimes(self.policy, Path(__file__).resolve().parent, self.runtime)
        self.addCleanup(self.tracker.close)

    def begin(self, obj):
        # Only unit boundary injection uses frame-shaped data. The native CLI
        # tests separately prove real monitoring callbacks and original code.
        code = objects.function_code(objects.resolved(type(obj), '__init__'))
        caller = SimpleNamespace(f_lasti=37, f_back=None)
        frame = SimpleNamespace(f_code=code, f_back=caller,
                                f_locals={code.co_varnames[0]: obj})
        self.tracker.class_call(caller, 37, type(obj))
        self.tracker.start(frame)
        return caller, frame

    def admit(self, obj):
        admitted = self.tracker.admitted
        caller, frame = self.begin(obj)
        self.tracker.finish(frame, None, None)
        self.assertEqual(self.tracker.admitted, admitted)
        self.tracker.class_finished(caller, 37, failed=False)

    def release(self, obj, value=None, error=None):
        code = objects.function_code(objects.resolved(type(obj), 'close'))
        frame = SimpleNamespace(f_code=code, f_back=None, f_locals={'self': obj})
        self.tracker.start(frame)
        self.tracker.finish(frame, value, error)

    def test_actual_weak_objects_resource_completion_and_gc_do_not_retain_receivers(self) -> None:
        obj = Item()
        reference = weakref.ref(obj)
        self.admit(obj)
        self.assertEqual(self.tracker.status()['current_live'], 1)
        self.release(obj, error=ValueError())
        self.release(obj)
        self.release(obj)
        self.assertEqual((self.tracker.failed_releases, self.tracker.resource_completed), (1, 1))
        del obj
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual((self.tracker.gc_completed, self.tracker.open_resources), (1, 0))
        self.assertTrue(any(row[0][1] == 'otelc.lifetime.duration' for row in self.runtime.metrics))
        self.assertTrue(all(row[1]['scope'] == 'io.quux.otelc.lifetimes' for row in self.runtime.metrics))

    def test_collection_off_requires_no_metric_or_tracing_admission(self) -> None:
        self.runtime.collection = False
        obj = Item()
        caller, frame = self.begin(obj)
        self.tracker.finish(frame, None, None)
        self.tracker.class_finished(caller, 37, failed=False)
        self.assertEqual((self.tracker.admitted, self.runtime.metrics), (0, []))
        self.runtime.collection = True
        self.admit(obj)
        self.assertEqual(self.tracker.admitted, 1)

    def test_failed_initialisation_is_counted_once_and_rebinding_self_is_loss(self) -> None:
        obj = Item()
        caller, frame = self.begin(obj)
        self.tracker.finish(frame, None, ValueError())
        self.tracker.class_finished(caller, 37, failed=True)
        self.assertEqual((self.tracker.failed_initialisations, self.tracker.admitted), (1, 0))
        caller, frame = self.begin(obj)
        frame.f_locals['self'] = Item()
        self.tracker.finish(frame, None, None)
        self.tracker.class_finished(caller, 37, failed=False)
        self.assertEqual(self.tracker.losses, {'receiver_changed': 1})

    def test_missing_initializer_and_weak_reference_are_explicit_losses(self) -> None:
        caller = SimpleNamespace(f_lasti=1)
        self.tracker.class_call(caller, 1, DefaultItem)
        self.tracker.class_finished(caller, 1, failed=False)
        obj = UnreferencedItem()
        self.admit(obj)
        self.assertEqual(self.tracker.losses,
                         {'unsupported_initializer': 1, 'unsupported_weak_reference': 1})
        self.assertEqual(self.tracker.admitted, 0)

    def test_capacity_and_token_exhaustion_do_not_fabricate_live_objects(self) -> None:
        first, second = Item(), Item()
        self.tracker.maximum = 1
        self.admit(first)
        self.admit(second)
        self.assertEqual(self.tracker.losses, {'lifetime_capacity': 1})
        self.assertEqual(len(self.tracker.instances), 1)
        self.tracker.maximum = 4096
        with mock.patch('otelc.python_lifetimes.MAX_TOKEN', self.tracker._next):
            self.admit(second)
        self.assertEqual(self.tracker.losses, {'lifetime_capacity': 2})

    def test_unreleased_gc_is_incomplete_and_a_returned_generator_is_not_release(self) -> None:
        obj = Item()
        self.admit(obj)
        generator = (value for value in ())
        self.release(obj, generator)
        generator.close()
        self.assertEqual(self.tracker.resource_completed, 0)
        self.assertEqual(self.tracker.losses, {'deferred_release_result': 1})
        del obj
        gc.collect()
        self.assertEqual(self.tracker.open_resources, 0)
        self.assertTrue(any(row[0][1] == 'otelc.lifetime.incomplete' for row in self.runtime.metrics))
        self.tracker._collected(999)
        self.assertEqual(self.tracker.losses['stale_completion'], 1)

    def test_duplicate_initialisation_and_borrowed_release_are_not_duplicate_boundaries(self) -> None:
        obj = Item()
        self.admit(obj)
        caller, frame = self.begin(obj)
        self.tracker.finish(frame, None, None)
        self.tracker.class_finished(caller, 37, failed=False)
        self.assertEqual(self.tracker.admitted, 1)
        code = Item.close.__code__
        frame = SimpleNamespace(f_code=code, f_back=None, f_locals={'self': obj})
        self.tracker.start(frame)
        frame.f_locals['self'] = Item()
        self.tracker.finish(frame, None, None)
        self.assertEqual(self.tracker.resource_completed, 0)
        self.assertEqual(self.tracker.losses['receiver_changed'], 1)

    def test_censoring_clears_bookkeeping_without_ending_live_spans(self) -> None:
        obj = Item()
        self.admit(obj)
        caller, frame = self.begin(Item())
        self.tracker.close()
        self.tracker.close()
        self.assertEqual(self.tracker.status()['censored'], 1)
        self.assertEqual(self.tracker.status()['tracked_instances'], 0)
        self.assertEqual(self.tracker.losses, {'incomplete_construction': 1})
        self.tracker.class_call(caller, 37, Item)
        self.tracker.start(frame)
        self.tracker._collected(1)
        self.assertEqual(self.tracker.gc_completed, 0)

    def test_raw_metadata_handles_nonclasses_missing_modules_and_variadic_receivers(self) -> None:
        def noargs():
            pass

        def variadic(*args):
            pass

        class Awaitable:
            def __await__(self):
                return iter(())

        self.assertFalse(objects.is_class(Item()))
        self.assertIsNone(objects.function_code(42))
        self.assertIsNone(objects.resolved(Item, 'missing'))
        self.assertIsNone(objects.module_filename(None))
        self.assertIsNone(objects.module_filename('not_an_installed_module'))
        self.assertIsNone(objects.receiver(SimpleNamespace(f_code=noargs.__code__, f_locals={})))
        frame = SimpleNamespace(f_code=variadic.__code__, f_locals={'args': (42,)})
        self.assertEqual(objects.receiver(frame), 42)
        frame.f_locals['args'] = ()
        self.assertIsNone(objects.receiver(frame))
        self.assertTrue(objects.deferred(Awaitable()))
        self.assertFalse(objects.deferred(42))

    def test_type_and_frame_caps_reject_without_replacing_existing_bookkeeping(self) -> None:
        caller = SimpleNamespace(f_lasti=37)
        with mock.patch('otelc.python_lifetimes.MAX_TYPES', 0):
            self.tracker.class_call(caller, 37, Item)
        with mock.patch('otelc.python_lifetimes.MAX_FRAMES', 0):
            self.tracker.class_call(caller, 37, Item)
        self.assertEqual(self.tracker.calls, {})
        self.assertEqual(self.tracker.losses, {'type_capacity': 1, 'constructor_capacity': 1})
        self.tracker._remove_type(id(Item))
        self.tracker._remove_type(id(Item))
        self.assertEqual(self.tracker.codes, {})

