"""Qualify private live ownership with original code and real monitoring events."""
import ast
import dis
import gc
import signal
import sys
import threading
from types import CodeType
import unittest
import weakref

from test_python_adapter import ROOT
from quux_otelc_python.witness_callsite import CallSite, bind_site
from quux_otelc_python.witness_code import CodeBindingError, CodeRegistry
from quux_otelc_python.witness_owner import ExecutionOwner, OwnerProfile


SOURCE = '''import signal
ERROR = ValueError('original')
def target(depth):
    if depth < 0:
        return 42
    if depth == 0:
        if FAIL:
            raise ERROR
        return 42
    return target(depth - 1)
def factory():
    target(-1)
    return 2
def wrapper():
    return target(factory())
def callback(_signal, _frame):
    return wrapper()
def rendezvous():
    pass
def envelope():
    if USE_SIGNAL:
        signal.raise_signal(signal.SIGUSR1)
    rendezvous()
    return wrapper()
if USE_SIGNAL:
    signal.signal(signal.SIGUSR1, callback)
RESULT = envelope()
'''


class PythonWitnessOwnerTests(unittest.TestCase):
    def unit_driver(self, *, fail=False, capacity=64):
        # Direct API drivers complement the unchanged-source callback controls.
        # CPython suspends coverage tracing in monitoring callbacks; these live
        # frames let the C tracer measure the observer without recursive tracing.
        bodies = {
            'target': ('depth', 'if depth < 0:\n    return 42\nif depth == 0:\n    if FAIL:\n        raise ERROR\n    return 42\nreturn target(depth - 1)'),
            'wrapper': ('', 'return target(factory())'),
            'rendezvous': ('', 'return None'),
            'envelope': ('', 'rendezvous()\nreturn wrapper()'),
            'primary': ('', 'return envelope()'),
        }
        source = "def factory():\n    target(-1)\n    return 2\n"
        for name, (arguments, body) in bodies.items():
            source += f'\ndef {name}({arguments}):\n    EVENTS.append(OWNER.entered(sys._getframe()))\n    try:\n'
            source += ''.join('        ' + line + '\n' for line in body.splitlines())
            source += "    finally:\n        EVENTS.append(OWNER.exited(sys._getframe(), 'exception' if sys.exception() is not None else 'return'))\n"
        namespace = {'sys': sys, 'EVENTS': [], 'FAIL': fail, 'ERROR': ValueError('original')}
        exec(compile(source, str(ROOT / 'owner_api_unit.py'), 'exec'), namespace)
        registry = CodeRegistry()
        codes = {name: namespace[name].__code__ for name in bodies}
        for code in codes.values():
            registry.register(code)
        tree = ast.parse(source)

        def site(caller, callee):
            definition = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == caller)
            call = next(node for node in ast.walk(definition) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name) and node.func.id == callee)
            position = (call.lineno, call.end_lineno, call.col_offset, call.end_col_offset)
            offsets = [instruction.offset for instruction in dis.get_instructions(codes[caller])
                       if instruction.opname == 'CALL' and tuple(instruction.positions) == position]
            self.assertEqual(len(offsets), 1)
            return bind_site(registry, codes[caller], codes[callee], offsets[0])

        profile = OwnerProfile(registry, codes['primary'], site('primary', 'envelope'),
                               site('envelope', 'rendezvous'), site('envelope', 'wrapper'), site('wrapper', 'target'))
        owner = ExecutionOwner(profile, capacity)
        namespace['OWNER'] = owner
        return namespace, owner

    def test_direct_api_drivers_measure_normal_unwind_and_capacity_with_real_frames(self):
        for fail, capacity in ((False, 64), (True, 64), (False, 1)):
            with self.subTest(fail=fail, capacity=capacity):
                namespace, owner = self.unit_driver(fail=fail, capacity=capacity)
                if fail:
                    with self.assertRaises(ValueError) as caught:
                        namespace['primary']()
                    self.assertIs(caught.exception, namespace['ERROR'])
                else:
                    self.assertEqual(namespace['primary'](), 42)
                events = [event for event in namespace['EVENTS'] if event is not None]
                self.assertTrue(any(event.kind == 'rendezvous' for event in events))
                self.assertEqual(owner.inflight, 0)
                if capacity == 1:
                    self.assertEqual(owner.fault, 'target_activation_capacity')
                else:
                    self.assertIsNone(owner.fault)
                    self.assertEqual(sum(event.kind == 'target_enter' for event in events), 3)
                self.assertIsNone(owner.close())

    def prepared(self, source=SOURCE, max_targets=64):
        root = compile(source, str(ROOT / 'witness_owner_original.py'), 'exec')
        codes = {code.co_name: code for code in root.co_consts if type(code) is CodeType}
        codes['primary'] = root
        registry = CodeRegistry()
        for code in codes.values():
            registry.register(code)

        def site(caller, callee, index=-1):
            calls = [instruction.offset for instruction in dis.get_instructions(codes[caller])
                     if instruction.opname == 'CALL']
            return bind_site(registry, codes[caller], codes[callee], calls[index])

        profile = OwnerProfile(registry, root, site('primary', 'envelope'),
                               site('envelope', 'rendezvous', -2), site('envelope', 'wrapper'),
                               site('wrapper', 'target'))
        return root, codes, registry, ExecutionOwner(profile, max_targets)

    def execute(self, root, owner, *, fail=False, signals=True, close_at=None, drop_exit_code=None):
        events, error = [], None
        namespace = {'FAIL': fail, 'USE_SIGNAL': signals}

        def collect(event):
            if event is not None:
                events.append(event)
                if event.kind == close_at:
                    closed = owner.close()
                    if closed is not None:
                        events.append(closed)

        def entered(_code, _offset):
            collect(owner.entered(sys._getframe(1)))

        def returned(code, _offset, _value):
            if code is not drop_exit_code:
                collect(owner.exited(sys._getframe(1), 'return'))

        def unwound(code, _offset, _error):
            if code is not drop_exit_code:
                collect(owner.exited(sys._getframe(1), 'exception'))

        monitoring = sys.monitoring
        tool = next(number for number in (3, 4, 2, 5) if monitoring.get_tool(number) is None)
        previous = signal.getsignal(signal.SIGUSR1)
        monitoring.use_tool_id(tool, 'otelc-private-owner-test')
        try:
            monitoring.register_callback(tool, monitoring.events.PY_START, entered)
            monitoring.register_callback(tool, monitoring.events.PY_RETURN, returned)
            monitoring.register_callback(tool, monitoring.events.PY_UNWIND, unwound)
            monitoring.set_events(tool, monitoring.events.PY_START | monitoring.events.PY_RETURN | monitoring.events.PY_UNWIND)
            try:
                exec(root, namespace)
            except ValueError as caught:
                self.assertIs(caught, namespace['ERROR'])
                error = caught
        finally:
            monitoring.set_events(tool, 0)
            for event in (monitoring.events.PY_START, monitoring.events.PY_RETURN, monitoring.events.PY_UNWIND):
                monitoring.register_callback(tool, event, None)
            monitoring.free_tool_id(tool)
            signal.signal(signal.SIGUSR1, previous)
        return namespace, events, error

    def test_actual_primary_owner_excludes_signal_and_factory_calls_and_retires(self):
        root, codes, _, owner = self.prepared()
        original = {name: code.co_code for name, code in codes.items()}
        namespace, events, error = self.execute(root, owner)
        self.assertIsNone(error)
        self.assertEqual(namespace['RESULT'], 42)
        kinds = [event.kind for event in events]
        self.assertEqual(kinds, ['primary_enter', 'envelope_enter', 'rendezvous', 'wrapper_enter',
                                 'target_enter', 'target_enter', 'target_enter', 'target_exit',
                                 'target_exit', 'target_exit', 'wrapper_exit', 'envelope_exit', 'primary_exit'])
        context = next(event.context for event in events if event.kind == 'rendezvous')
        self.assertTrue(all(event.context is context for event in events[1:-1]))
        wrapper = next(event for event in events if event.kind == 'wrapper_enter')
        self.assertIs(next(event.activation for event in events if event.kind == 'wrapper_exit'), wrapper.activation)
        self.assertEqual(owner.inflight, 0)
        self.assertIsNone(owner.fault)
        self.assertIsNone(owner.close())
        self.assertIsNone(owner.close())
        self.assertEqual({name: code.co_code for name, code in codes.items()}, original)

    def test_exception_unwind_retires_the_same_owner_and_original_exception(self):
        root, _, _, owner = self.prepared()
        namespace, events, error = self.execute(root, owner, fail=True, signals=False)
        self.assertIs(error, namespace['ERROR'])
        exits = [event for event in events if event.kind.endswith('_exit')]
        self.assertEqual(len(exits), 6)
        self.assertTrue(all(event.outcome == 'exception' for event in exits))
        self.assertIsNone(owner.fault)
        self.assertEqual(owner.inflight, 0)
        self.assertIsNone(owner.close())

    def test_equal_primary_clone_and_wrong_thread_cannot_steal_first_entry(self):
        root, _, _, owner = self.prepared()
        clone = root.replace()
        self.assertEqual(root, clone)
        namespace, events, _ = self.execute(clone, owner, signals=False)
        self.assertEqual(namespace['RESULT'], 42)
        self.assertFalse(events)
        worker_errors = []
        worker_events = []
        worker_results = []

        def worker():
            try:
                namespace = {'FAIL': False, 'USE_SIGNAL': False}
                exec(root, namespace)
                worker_results.append(namespace['RESULT'])
            except BaseException as error:
                worker_errors.append(error)

        # Installing signal handlers is main-thread-only; the worker uses an
        # ordinary exec while the same actual callback observes all threads.
        def run_worker(_code, _offset):
            event = owner.entered(sys._getframe(1))
            if event is not None:
                worker_events.append(event)

        tool = next(number for number in (3, 4, 2, 5) if sys.monitoring.get_tool(number) is None)
        sys.monitoring.use_tool_id(tool, 'otelc-private-owner-worker-test')
        try:
            sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, run_worker)
            sys.monitoring.set_events(tool, sys.monitoring.events.PY_START)
            thread = threading.Thread(target=worker)
            thread.start(); thread.join(3)
            self.assertFalse(thread.is_alive())
        finally:
            sys.monitoring.set_events(tool, 0)
            sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, None)
            sys.monitoring.free_tool_id(tool)
        self.assertFalse(worker_errors)
        self.assertFalse(worker_events)
        self.assertEqual(worker_results, [42])
        self.assertFalse(owner.primary_seen)
        namespace, events, _ = self.execute(root, owner, signals=False)
        self.assertEqual(namespace['RESULT'], 42)
        self.assertTrue(any(event.kind == 'wrapper_enter' for event in events))

    def test_reentry_capacity_and_early_close_remain_visible_failures(self):
        for mode in ('primary', 'envelope', 'capacity', 'close'):
            with self.subTest(mode=mode):
                source = SOURCE.replace('RESULT = envelope()', 'for _ in range(2):\n    RESULT = envelope()') if mode == 'envelope' else SOURCE
                root, _, _, owner = self.prepared(source, max_targets=1 if mode == 'capacity' else 64)
                namespace, events, _ = self.execute(root, owner, signals=False,
                                                    close_at='rendezvous' if mode == 'close' else None)
                if mode == 'primary':
                    namespace, later, _ = self.execute(root, owner, signals=False)
                    events.extend(later)
                self.assertEqual(namespace['RESULT'], 42)
                expected = {'primary': 'primary_reentered', 'envelope': 'context_reused',
                            'capacity': 'target_activation_capacity', 'close': 'incomplete_execution'}[mode]
                self.assertEqual(owner.fault, expected)
                self.assertEqual([event.outcome for event in events if event.kind == 'invalidated'], [expected])
                owner.close()
                self.assertEqual(owner.inflight, 0)

    def test_profile_refusals_and_registry_do_not_retain_application_code(self):
        root, codes, registry, owner = self.prepared()
        with self.assertRaisesRegex(CodeBindingError, 'unregistered_primary_code'):
            OwnerProfile(registry, root.replace(), owner.profile.envelope, owner.profile.rendezvous,
                         owner.profile.wrapper, owner.profile.target)
        forged = CallSite(lambda: root, lambda: root, 0, 0, '', '', '')
        with self.assertRaisesRegex(CodeBindingError, 'unsupported_owner_site'):
            OwnerProfile(registry, root, forged, owner.profile.rendezvous, owner.profile.wrapper, owner.profile.target)
        for capacity in (True, 0, 4097):
            with self.assertRaisesRegex(CodeBindingError, 'unsupported_execution_owner'):
                ExecutionOwner(owner.profile, capacity)
        reference = weakref.ref(root)
        del root, codes, forged
        gc.collect()
        self.assertIsNone(reference())
        self.assertIsNone(owner.close())
        self.assertIsNone(owner.entered(object()))
        self.assertIsNone(owner.exited(object(), 'return'))

    def test_missing_child_completion_invalidates_before_primary_retirement(self):
        for role in ('target', 'envelope'):
            with self.subTest(role=role):
                root, codes, _, owner = self.prepared()
                namespace, events, _ = self.execute(root, owner, signals=False, drop_exit_code=codes[role])
                self.assertEqual(namespace['RESULT'], 42)
                self.assertEqual(owner.fault, 'unbalanced_execution')
                self.assertEqual([event.outcome for event in events if event.kind == 'invalidated'], ['unbalanced_execution'])
                self.assertIsNone(owner.primary)
                self.assertIsNone(owner.envelope)
                owner.close()
                self.assertEqual(owner.inflight, 0)
