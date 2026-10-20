"""Use actual monitoring callbacks to reject same-thread, same-envelope signals."""
import dis
import inspect
import signal
import sys
import threading
from types import FunctionType
import unittest
from unittest.mock import patch

from test_python_adapter import ROOT
from quux_otelc_python.witness_code import CodeBindingError, CodeRegistry
from quux_otelc_python.witness_callsite import bind_site


SOURCE = '''import signal
def target():
    return 42
def wrapper():
    return target()
def callback(_signal, _frame):
    return wrapper()
def envelope():
    signal.raise_signal(signal.SIGUSR1)
    return wrapper()
'''


class PythonWitnessCallsiteTests(unittest.TestCase):
    def prepared(self):
        namespace = {}
        root = compile(SOURCE, str(ROOT / 'witness_original.py'), 'exec')
        exec(root, namespace)
        caller, callee = namespace['envelope'].__code__, namespace['wrapper'].__code__
        # Test fixture metadata contains ordinary builtin constants only. The
        # production site binder never calls dis, which may render constants.
        calls = [instruction.offset for instruction in dis.get_instructions(caller)
                 if instruction.opname == 'CALL']
        registry = CodeRegistry()
        registry.register(caller); registry.register(callee)
        return namespace, caller, callee, registry, calls[-1]

    def observed(self, namespace, site):
        observations = []

        def entered(code, _offset):
            if code.co_name == 'wrapper':
                frame = sys._getframe(1)
                ancestor = frame.f_back
                while ancestor is not None and ancestor.f_code.co_name != 'envelope':
                    ancestor = ancestor.f_back
                observations.append((site.matches(frame), threading.get_native_id(), id(ancestor)))

        tool = next(number for number in (3, 4, 2, 5) if sys.monitoring.get_tool(number) is None)
        previous = signal.signal(signal.SIGUSR1, namespace['callback'])
        sys.monitoring.use_tool_id(tool, 'otelc-private-callsite-test')
        try:
            sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, entered)
            sys.monitoring.set_events(tool, sys.monitoring.events.PY_START)
            self.assertEqual(namespace['envelope'](), 42)
        finally:
            sys.monitoring.set_events(tool, 0)
            sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, None)
            sys.monitoring.free_tool_id(tool)
            signal.signal(signal.SIGUSR1, previous)
        return observations

    def test_signal_and_intended_entry_share_thread_and_envelope_but_only_site_matches(self):
        namespace, caller, callee, registry, offset = self.prepared()
        site = bind_site(registry, caller, callee, offset)
        observed = self.observed(namespace, site)
        self.assertEqual([entry[0] for entry in observed], [False, True])
        self.assertEqual(len({entry[1] for entry in observed}), 1)
        self.assertEqual(len({entry[2] for entry in observed}), 1)
        self.assertIs(namespace['envelope'].__code__, caller)
        self.assertIs(namespace['wrapper'].__code__, callee)
        self.assertEqual(site.record()['call_offset'], offset)
        self.assertEqual(site.record()['caller_probe_id'], registry.lookup(caller).digest)
        self.assertFalse(site.matches(object()))

    def test_equal_caller_or_callee_clones_cannot_satisfy_the_site(self):
        for name in ('envelope', 'wrapper'):
            with self.subTest(name=name):
                namespace, caller, callee, registry, offset = self.prepared()
                site = bind_site(registry, caller, callee, offset)
                original = namespace[name]
                clone = original.__code__.replace()
                self.assertEqual(original.__code__, clone)
                namespace[name] = FunctionType(clone, namespace)
                self.assertEqual([entry[0] for entry in self.observed(namespace, site)], [False, False])

    def test_wrong_offsets_shapes_and_versions_refuse_before_execution(self):
        namespace, caller, callee, registry, offset = self.prepared()
        for position in (True, -2, offset + 1, len(caller.co_code), 0):
            with self.subTest(position=position), self.assertRaisesRegex(CodeBindingError, 'invalid_callsite_offset'):
                bind_site(registry, caller, callee, position)
        for unregistered in (caller.replace(), object()):
            with self.assertRaises(CodeBindingError):
                bind_site(registry, unregistered, callee, offset)
        with patch.object(sys, 'version_info', (3, 13, 0)), self.assertRaisesRegex(CodeBindingError, 'unsupported_callsite_profile'):
            bind_site(registry, caller, callee, offset)
        for flags in (inspect.CO_GENERATOR, inspect.CO_COROUTINE, inspect.CO_ASYNC_GENERATOR):
            modified = caller.replace(co_flags=caller.co_flags | flags)
            registry.register(modified)
            with self.assertRaisesRegex(CodeBindingError, 'unsupported_callsite_shape'):
                bind_site(registry, modified, callee, offset)
        shortened = caller.replace(co_code=caller.co_code[:offset + 2])
        registry.register(shortened)
        with self.assertRaisesRegex(CodeBindingError, 'unsupported_callsite_shape'):
            bind_site(registry, shortened, callee, offset)
