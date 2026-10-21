"""Qualified refusal metadata and live-wrapper ownership controls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_python_witness_owner as owner_tests
from quux_otelc_python import witness_owner
from quux_otelc_python.monitor import Monitor
from quux_otelc_python.witness_code import CodeBindingError
from quux_otelc_python.witness_continuations import WitnessContinuations, unsupported_callable
import test_python_witness_state as state_tests


def generator():
    yield 1


async def coroutine():
    return 1


async def async_generator():
    yield 1


class PythonWitnessContinuationTests(unittest.TestCase):
    def test_exact_functions_bound_methods_partial_and_standard_handoffs(self):
        class Methods:
            async def deferred(self):
                return 1

        for function in (generator, coroutine, async_generator, Methods().deferred,
                         functools.partial(coroutine), threading.Thread.start,
                         ThreadPoolExecutor.submit, asyncio.BaseEventLoop.call_soon):
            with self.subTest(function=function):
                self.assertTrue(unsupported_callable(function))
        self.assertFalse(unsupported_callable(lambda: 1))
        self.assertFalse(unsupported_callable(list))
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        future = loop.create_future()
        self.assertTrue(unsupported_callable(future.add_done_callback))
        self.assertTrue(unsupported_callable(asyncio.Future.add_done_callback))
        self.assertFalse(unsupported_callable(future.done))
        task = loop.create_task(coroutine())
        self.assertTrue(unsupported_callable(task.add_done_callback))
        self.assertEqual(1, loop.run_until_complete(task))
        nested = coroutine
        for _ in range(10):
            nested = functools.partial(nested)
            nested.keep_metadata = True
        self.assertTrue(unsupported_callable(nested))

    def test_callable_metadata_never_dispatches_application_hooks(self):
        class Trap:
            def __getattribute__(self, name):
                raise AssertionError('application attribute hook')

            def __eq__(self, other):
                raise AssertionError('application comparison')

            def __hash__(self):
                raise AssertionError('application hash')

        self.assertFalse(unsupported_callable(Trap()))

    def guard(self, *, inside=True, fail=False):
        state = state_tests.PythonWitnessStateTests().state()
        def contains(frame):
            if fail:
                raise CodeBindingError('owner_frame_capacity')
            return inside
        return WitnessContinuations(SimpleNamespace(state=state, contains=contains)), state

    def test_refusal_is_scoped_permanent_and_does_not_invoke_the_callable(self):
        for inside in (False, True):
            guard, state = self.guard(inside=inside)
            guard.called(sys._getframe(), coroutine)
            self.assertEqual('unsupported_profile' if inside else None, state.fault)
            count = state.fault_count
            guard.called(sys._getframe(), coroutine)
            guard.execution(sys._getframe(), generator.__code__)
            self.assertEqual(count, state.fault_count)
        guard, state = self.guard()
        guard.execution(sys._getframe(), self.guard.__code__)
        self.assertIsNone(state.fault)
        guard.execution(sys._getframe(), generator.__code__)
        self.assertEqual('unsupported_profile', state.fault)
        guard, state = self.guard(fail=True)
        guard.called(sys._getframe(), coroutine)
        self.assertEqual('incomplete', state.fault)

    def test_monitor_entry_call_resume_yield_and_throw_forward_the_real_frame(self):
        # Separate direct API driver for coverage; the real CLI cases establish
        # actual CPython callback behaviour without recursive coverage tracing.
        guard, state = self.guard()
        monitor = object.__new__(Monitor)
        monitor.continuations, monitor.context = guard, None
        monitor.called(None, 0, coroutine, None)
        self.assertEqual('unsupported_profile', state.fault)
        for method, args in ((monitor.resumed, (generator.__code__, 0)),
                             (monitor.yielded, (generator.__code__, 0, None)),
                             (monitor.thrown, (generator.__code__, 0, ValueError()))):
            guard, state = self.guard()
            monitor.continuations = guard
            method(*args)
            self.assertEqual('unsupported_profile', state.fault)

    def test_actual_owner_requires_live_wrapper_ancestry_and_native_thread(self):
        namespace, owner = owner_tests.PythonWitnessOwnerTests().unit_driver()
        observed = []
        entered = owner.entered
        def enter(frame):
            event = entered(frame)
            observed.append((frame.f_code.co_name, owner.contains(frame)))
            if event is not None and event.kind == 'wrapper_enter':
                other = threading.Thread(target=lambda: observed.append(('other_thread', owner.contains(frame))))
                other.start(); other.join(5)
                self.assertFalse(other.is_alive())
            return event
        owner.entered = enter
        self.assertFalse(owner.contains(object()))
        self.assertEqual(42, namespace['primary']())
        self.assertIn(('wrapper', True), observed)
        self.assertIn(('target', True), observed)
        self.assertIn(('primary', False), observed)
        self.assertIn(('other_thread', False), observed)
        self.assertFalse(owner.contains(sys._getframe()))
        owner.close()
        self.assertFalse(owner.contains(sys._getframe()))

    def test_owner_ancestor_scan_is_bounded_and_refuses_uncertainty(self):
        namespace, owner = owner_tests.PythonWitnessOwnerTests().unit_driver()
        def factory():
            with patch.object(witness_owner, 'range', return_value=range(1), create=True):
                with self.assertRaisesRegex(CodeBindingError, 'owner_frame_capacity'):
                    owner.contains(sys._getframe())
            return 2
        namespace['factory'] = factory
        self.assertEqual(42, namespace['primary']())
        self.assertIsNone(owner.close())
