"""Bind actual monitoring entries to decoded SDK spans, without manual probes."""
import ast
import dis
import sys
import threading
import time
from types import CodeType
import unittest

from test_python_adapter import Capture, ROOT
from test_python_spans import TraceCapture, trace_plan
import test_python_witness_state as state_tests
import test_python_witness_owner as owner_tests
from quux_otelc_python.monitor import Monitor
from quux_otelc_python.telemetry import Runtime
from quux_otelc_python.witness_callsite import bind_site
from quux_otelc_python.witness_code import CodeBindingError, CodeRegistry
from quux_otelc_python.witness_owner import OwnerProfile
from quux_otelc_python.witness_sdk import WitnessSDK
from quux_otelc_python.witness_state import Bindings, WitnessState
from quux_otelc_python.witness_wire import decode_record
from opentelemetry.sdk.trace.export import SpanExportResult


SOURCE = '''def target(depth):
    if depth > 0:
        return target(depth - 1)
    if FAIL and depth == 0:
        raise ERROR
    return 42
def factory():
    target(-1)
    if FACTORY_FAIL:
        raise ERROR
    return 2
def wrapper():
    return target(factory())
def rendezvous():
    if not RELEASE.wait(5):
        raise TimeoutError('harness release')
def envelope():
    rendezvous()
    return wrapper()
try:
    RESULT = envelope()
except ValueError as error:
    CAUGHT = error
'''


class PythonWitnessSDKTests(unittest.TestCase):
    def test_wrong_private_state_or_code_registry_cannot_install_sdk_binding(self):
        namespace, owner = owner_tests.PythonWitnessOwnerTests().unit_driver()
        runtime = Runtime(trace_plan(), Capture(), TraceCapture())
        self.addCleanup(runtime.close)
        with self.assertRaisesRegex(CodeBindingError, '^missing_witness_state$'):
            WitnessSDK(runtime, CodeRegistry(), owner.profile)
        state = state_tests.PythonWitnessStateTests().state()
        runtime = Runtime(trace_plan(), Capture(), TraceCapture(), witness_state=state)
        self.addCleanup(runtime.close)
        with self.assertRaisesRegex(CodeBindingError, '^mismatched_witness_probes$'):
            WitnessSDK(runtime, CodeRegistry(), owner.profile)
        self.assertIsNotNone(namespace['primary'])  # Keep original weak code bindings live.

    def direct_driver(self, *, fail=False, ratio=1.0, capacity=32, skip_root=False):
        # Coverage is suspended in CPython monitoring callbacks. These separate
        # API controls use actual live frames; the unchanged source test below
        # establishes real callback behaviour without recursive coverage tracing.
        bodies = {
            'target': ('depth', 'if depth > 0:\n    return target(depth - 1)\nif FAIL and depth == 0:\n    raise ERROR\nreturn 42'),
            'wrapper': ('', 'return target(factory())'),
            'rendezvous': ('', 'ARM()'),
            'envelope': ('', 'rendezvous()\nreturn wrapper()'),
            'primary': ('', 'return envelope()'),
        }
        source = 'def factory():\n    target(-1)\n    return 2\n'
        for name, (arguments, body) in bodies.items():
            source += f'\ndef {name}({arguments}):\n    MONITOR.start(sys._getframe().f_code, 0)\n    try:\n'
            source += ''.join('        ' + line + '\n' for line in body.splitlines())
            source += "    finally:\n        if sys.exception() is None:\n            MONITOR.returned(sys._getframe().f_code, 0, None)\n        else:\n            MONITOR.unwound(sys._getframe().f_code, 0, sys.exception())\n"
        namespace = {'sys': sys, 'FAIL': fail, 'ERROR': ValueError('original')}
        exec(compile(source, str(ROOT / 'trace_fixture.py'), 'exec'), namespace)
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
            offsets = [item.offset for item in dis.get_instructions(codes[caller])
                       if item.opname == 'CALL' and tuple(item.positions) == position]
            self.assertEqual(1, len(offsets))
            return bind_site(registry, codes[caller], codes[callee], offsets[0])

        profile = OwnerProfile(registry, codes['primary'], site('primary', 'envelope'),
                               site('envelope', 'rendezvous'), site('envelope', 'wrapper'), site('wrapper', 'target'))
        bindings = Bindings('a' * 64, 'b' * 64, registry.lookup(codes['rendezvous']),
                            registry.lookup(codes['wrapper']), registry.lookup(codes['target']), object())
        state = WitnessState(bindings, available=True)
        policy = trace_plan()
        policy['function_matchers']['include'] = [r'(?-u)^trace_fixture\.(wrapper|target)$']
        policy['traces']['root_sample_ratio'] = ratio
        policy['traces']['max_spans_per_trace'] = capacity
        if skip_root:
            policy['function_matchers']['include'] = [r'(?-u)^trace_fixture\.target$']
        traces = TraceCapture()
        runtime = Runtime(policy, Capture(), traces, witness_state=state)
        self.addCleanup(runtime.close)
        sdk = WitnessSDK(runtime, registry, profile)
        monitor = Monitor(policy, runtime, witness=sdk)
        namespace['MONITOR'] = monitor
        namespace['ARM'] = lambda: self.assertTrue(state_tests.PythonWitnessStateTests().arm(state)['ok'])
        self.assertTrue(sdk.watches(codes['target']))
        self.assertFalse(sdk.watches(self.direct_driver.__code__))
        if fail:
            with self.assertRaises(ValueError) as caught:
                namespace['primary']()
            self.assertIs(namespace['ERROR'], caught.exception)
        else:
            self.assertEqual(42, namespace['primary']())
        sdk.close()
        report = runtime.close()
        return state, [decode_record(frame) for frame in state.drain()], report, traces

    def test_direct_api_controls_measure_real_frames_and_refuse_missing_admission(self):
        for change in ({}, {'fail': True}, {'ratio': 0.0}, {'capacity': 2}, {'skip_root': True}):
            with self.subTest(change=change):
                state, events, _, traces = self.direct_driver(**change)
                complete = not any(key in change for key in ('ratio', 'capacity', 'skip_root'))
                self.assertEqual(complete, events[-1]['complete'])
                if complete:
                    self.assertEqual(3, state.target_entries)
                    self.assertEqual(5, len(traces.spans()))

    def exercise(self, *, fail=False, factory_fail=False, ratio=1.0, capacity=32, export_ok=True, armed=True):
        root = compile(SOURCE, str(ROOT / 'trace_fixture.py'), 'exec')
        codes = {code.co_name: code for code in root.co_consts if type(code) is CodeType}
        codes['primary'] = root
        registry = CodeRegistry()
        for code in codes.values():
            registry.register(code)

        def site(caller, callee, index=-1):
            offsets = [item.offset for item in dis.get_instructions(codes[caller]) if item.opname == 'CALL']
            return bind_site(registry, codes[caller], codes[callee], offsets[index])

        profile = OwnerProfile(registry, root, site('primary', 'envelope'),
                               site('envelope', 'rendezvous', 0), site('envelope', 'wrapper'),
                               site('wrapper', 'target'))
        bindings = Bindings('a' * 64, 'b' * 64, registry.lookup(codes['rendezvous']),
                            registry.lookup(codes['wrapper']), registry.lookup(codes['target']), object())
        state = WitnessState(bindings, available=True)
        policy = trace_plan()
        policy['function_matchers']['include'] = [r'(?-u)^trace_fixture\.(wrapper|target)$']
        policy['traces']['root_sample_ratio'] = ratio
        policy['traces']['max_spans_per_trace'] = capacity
        metrics, traces = Capture(), TraceCapture()
        traces.result = SpanExportResult.SUCCESS if export_ok else SpanExportResult.FAILURE
        runtime = Runtime(policy, metrics, traces, witness_state=state)
        self.addCleanup(runtime.close)
        sdk = WitnessSDK(runtime, registry, profile)
        monitor = Monitor(policy, runtime, witness=sdk)
        self.addCleanup(monitor.close)
        release = threading.Event()
        acknowledgements = []

        def consumer():
            deadline = time.monotonic() + 4
            while state.phase == 'idle' and time.monotonic() < deadline:
                time.sleep(0.001)
            if armed:
                acknowledgements.append(state_tests.PythonWitnessStateTests().arm(state))
            release.set()

        worker = threading.Thread(target=consumer)
        worker.start()
        namespace = {'FAIL': fail, 'FACTORY_FAIL': factory_fail,
                     'ERROR': ValueError('original'), 'RELEASE': release}
        baseline_release = threading.Event()
        baseline_release.set()
        baseline = dict(namespace, RELEASE=baseline_release)
        exec(root, baseline)
        monitor.install()
        try:
            exec(root, namespace)
        finally:
            monitor.close()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        report = runtime.close()
        events = [decode_record(frame) for frame in state.drain()]
        self.assertEqual(1 if armed else 0, len(acknowledgements))
        if armed:
            self.assertTrue(acknowledgements[0]['ok'])
        if fail or factory_fail:
            self.assertIs(namespace['ERROR'], namespace['CAUGHT'])
        else:
            self.assertEqual(42, namespace['RESULT'])
        self.assertEqual(baseline.get('RESULT'), namespace.get('RESULT'))
        if fail or factory_fail:
            self.assertIs(baseline['CAUGHT'], namespace['CAUGHT'])
        self.assertIs(root, codes['primary'])
        return state, events, report, traces, metrics

    def test_actual_sdk_root_ids_and_only_intended_recursive_entries_are_tagged(self):
        state, events, report, traces, metrics = self.exercise()
        self.assertTrue(events[-1]['complete'])
        self.assertEqual(['ready', 'consumed', 'root_exit', 'end', 'runtime_end'],
                         [event['event'] for event in events])
        self.assertEqual(3, state.target_entries)  # The factory's separate call is unowned.
        spans = traces.spans()
        self.assertEqual(5, len(spans))
        tagged = []
        for span in spans:
            attributes = {value.key: value.value.string_value for value in span.attributes}
            if 'otelc.scope.id' in attributes:
                tagged.append((span, attributes))
        self.assertEqual(4, len(tagged))
        root, attributes = next(pair for pair in tagged if pair[0].name.endswith('.wrapper'))
        self.assertFalse(root.parent_span_id)
        self.assertEqual(state.trace_id, root.trace_id.hex())
        self.assertEqual(state.span_id, root.span_id.hex())
        self.assertEqual(state.bindings.wrapper.digest, attributes['otelc.probe.id'])
        self.assertTrue(all(attrs['otelc.scope.id'] == state.scope_id for _, attrs in tagged))
        resource = traces.requests[0].resource_spans[0].resource
        identity = {item.key: item.value.string_value for item in resource.attributes}
        self.assertEqual(state.runtime_id, identity['otelc.runtime.instance'])
        self.assertEqual(state.bindings.build_id, identity['otelc.build.id'])
        self.assertEqual(state.bindings.selection_id, identity['otelc.selection.id'])
        self.assertFalse(report['traces']['losses'])
        self.assertTrue(metrics.batches)
        for batch in metrics.batches:
            self.assertFalse(any(key.startswith('otelc.') for key in batch.resource_metrics[0].resource.attributes))

    def test_original_exception_remains_an_exception_outcome_with_complete_error_spans(self):
        _, events, _, traces, _ = self.exercise(fail=True)
        self.assertTrue(events[-1]['complete'])
        self.assertEqual('exception', events[2]['outcome'])
        self.assertEqual(4, sum(span.status.code == 2 for span in traces.spans()))

    def test_unsampled_capacity_export_failure_and_unarmed_runs_cannot_complete(self):
        for change in ({'ratio': 0.0}, {'capacity': 2}, {'export_ok': False}, {'armed': False},
                       {'factory_fail': True}):
            with self.subTest(change=change):
                _, events, _, _, _ = self.exercise(**change)
                self.assertFalse(events[-1]['complete'])
                self.assertTrue(any(event['event'] == 'invalidated' for event in events))
