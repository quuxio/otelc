"""Instrumentation must not execute application metadata hooks."""
import unittest

from test_python_lifetimes import collection_plan
from test_python_spans import PythonSpanHarness, trace_plan


STRING_HOOKS = '''
seen = []
class Name(str):
    def __str__(self):
        seen.append('str')
        return str.__str__(self)
    def __radd__(self, prefix):
        seen.append('radd')
        return str.__add__(prefix, str.__str__(self))
    def startswith(self, prefix):
        seen.append('startswith')
        return str.startswith(self, prefix)
    def split(self, *args):
        seen.append('split')
        return str.split(self, *args)
    def __ne__(self, other):
        seen.append('ne')
        return str.__ne__(self, other)
    def __eq__(self, other):
        seen.append('eq')
        return str.__eq__(self, other)
    def __hash__(self):
        seen.append('hash')
        return str.__hash__(self)
'''


class PythonMetadataTests(PythonSpanHarness, unittest.TestCase):
    def test_builtin_namespace_lookup_does_not_dispatch_key_hooks(self):
        from quux_otelc_python.metadata import member, text
        app = {}
        exec(STRING_HOOKS, app)
        name = app['Name']('__init__')
        value = object()
        namespace = {name: value, 7: 'non-string', '__del__': None}
        app['seen'].clear()
        self.assertIs(member(namespace, '__init__'), value)
        self.assertIsNone(member(namespace, '__del__', value))
        self.assertIs(member(namespace, '__new__', value), value)
        self.assertIsNone(member(namespace, 'absent'))
        self.assertIs(type(text(name)), str)
        self.assertEqual(app['seen'], [])

    def test_class_qualname_string_subclass_is_passive_and_still_selected(self):
        policy = collection_plan()
        policy['function_matchers'] = {'include': [], 'exclude': []}
        runtime, _, capture, app, monitor = self.monitored(STRING_HOOKS + '''
class Resource:
    def __init__(self):
        self.value = 7
Resource.__qualname__ = Name('Resource')
''', policy)
        app['seen'].clear()
        value = app['Resource']()
        self.assertEqual(value.value, 7)
        del value
        report = self.finish(runtime, monitor)
        self.assertEqual(app['seen'], [])
        self.assertEqual(report['lifetimes']['admitted'], 1)
        self.assertEqual(report['lifetimes']['completed'], 1)
        self.assertFalse(report['lifetimes']['losses'])
        self.assertEqual([span.name for span in capture.spans()], ['trace_fixture.Resource lifetime'])

    def test_code_metadata_string_subclasses_do_not_call_application_hooks(self):
        for field in ('co_name', 'co_qualname', 'co_filename'):
            with self.subTest(field=field):
                runtime, _, capture, app, monitor = self.monitored(STRING_HOOKS + '''
def work(value):
    return value + 1
''', trace_plan())
                function = app['work']
                function.__code__ = function.__code__.replace(**{
                    field: app['Name'](getattr(function.__code__, field))})
                # Coverage/debugger tools may themselves inspect filenames. Use
                # the same warmed function without otelc as the control.
                monitor.close()
                self.assertEqual(function(6), 7)
                app['seen'].clear()
                self.assertEqual(function(6), 7)
                expected = list(app['seen'])
                app['seen'].clear()
                monitor.install()
                self.assertEqual(function(6), 7)
                report = self.finish(runtime, monitor)
                self.assertEqual(app['seen'], expected)
                self.assertEqual(report['function_calls'], 1)
                self.assertFalse(any(report['losses'].values()))
                self.assertEqual([span.name for span in capture.spans()], ['trace_fixture.work'])

    def test_class_namespace_keys_add_no_equality_or_hash_callbacks(self):
        policy = collection_plan()
        policy['function_matchers'] = {'include': [], 'exclude': []}
        runtime, _, _, app, monitor = self.monitored(STRING_HOOKS + '''
def __init__(self):
    self.value = 7
Resource = type('Resource', (), {Name('__init__'): __init__})
Baseline = type('Resource', (), {Name('__init__'): __init__})
''', policy)
        monitor.close()
        # Use separate fresh classes: the first ordinary constructor lookup can
        # compare its key, whereas later calls may hit CPython's type cache.
        app['seen'].clear()
        baseline = app['Baseline']()
        self.assertEqual(baseline.value, 7)
        del baseline
        expected = list(app['seen'])
        app['seen'].clear()
        monitor.install()
        value = app['Resource']()
        self.assertEqual(value.value, 7)
        del value
        report = self.finish(runtime, monitor)
        self.assertEqual(app['seen'], expected)
        self.assertEqual(report['lifetimes']['admitted'], 0)
        self.assertEqual(report['lifetimes']['completed'], 0)
        self.assertEqual(report['lifetimes']['losses'], {'unsupported_metadata': 1})
