"""Freeze real sources, then bind ordinary imports without preloading targets."""
import ast
import builtins
import contextlib
import dis
import io
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import CodeType
import unittest
from unittest.mock import patch

from test_python_adapter import Capture
from test_python_spans import TraceCapture, trace_plan
import test_python_witness_state as state_tests
from quux_otelc_python.monitor import Monitor
from quux_otelc_python.telemetry import Runtime
from quux_otelc_python.witness_bootstrap import WitnessBootstrap, _member, _qualified_loader
from importlib.machinery import SourceFileLoader
from quux_otelc_python.witness_code import CodeBindingError
from quux_otelc_python.witness_manifest import FrozenProgram
from quux_otelc_python import witness_manifest
from quux_otelc_python.witness_wire import decode_record


PROVIDER = "print('provider loaded')\ndef target(value):\n    return value + 1\n"


def harness(lazy=False, control='', loader_clone=False):
    imports = '' if lazy else 'import otelc_witness_provider\n'
    if loader_clone:
        imports = '''from importlib.machinery import SourceFileLoader
from types import CodeType
ORIGINAL_GET_CODE = SourceFileLoader.get_code
def cloned_get_code(loader, name):
    code = ORIGINAL_GET_CODE(loader, name)
    return code.replace(co_consts=tuple(value.replace() if type(value) is CodeType else value for value in code.co_consts))
SourceFileLoader.get_code = cloned_get_code
try:
    import otelc_witness_provider
finally:
    SourceFileLoader.get_code = ORIGINAL_GET_CODE
'''
        if loader_clone == 'hidden':
            imports = imports.replace('SourceFileLoader.get_code = cloned_get_code',
                "if 'get_code' in SourceFileLoader.__dict__:\n    del SourceFileLoader.get_code\nfor index in range(65):\n    setattr(SourceFileLoader, '_otelc_extra_' + str(index), True)\nSourceFileLoader.get_code = cloned_get_code")
            imports += "for index in range(65):\n    delattr(SourceFileLoader, '_otelc_extra_' + str(index))\ndel SourceFileLoader.get_code\n"
    deferred = '    global otelc_witness_provider\n    import otelc_witness_provider\n' if lazy else ''
    return imports + control + '''def rendezvous():
    if not RELEASE.wait(5):
        raise TimeoutError('harness release')
def wrapper():
    return otelc_witness_provider.target(41)
def envelope():
''' + deferred + '''    rendezvous()
    return wrapper()
RESULT = envelope()
print(RESULT)
'''


def declaration(root: Path, source: str):
    """Fixture compiler analysis only; runtime binding uses different objects."""
    compiled = compile(source, str(root / 'harness.py'), 'exec')
    codes = {value.co_name: value for value in compiled.co_consts if type(value) is CodeType}
    codes['primary'] = compiled
    tree = ast.parse(source)
    offsets = {}
    for edge, caller, callee in (('envelope', 'primary', 'envelope'),
                                  ('rendezvous', 'envelope', 'rendezvous'),
                                  ('wrapper', 'envelope', 'wrapper'), ('target', 'wrapper', 'target')):
        owner = tree if caller == 'primary' else next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == caller)
        call = next(node for node in ast.walk(owner) if isinstance(node, ast.Call)
                    and (isinstance(node.func, ast.Name) and node.func.id == callee
                         or isinstance(node.func, ast.Attribute) and node.func.attr == callee))
        position = (call.lineno, call.end_lineno, call.col_offset, call.end_col_offset)
        matches = [item.offset for item in dis.get_instructions(codes[caller])
                   if item.opname == 'CALL' and tuple(item.positions) == position]
        if len(matches) != 1:
            raise AssertionError('fixture needs a unique compiled call site')
        offsets[edge] = matches[0]
    roles = {role: {'path': 'harness.py', 'function': role} for role in ('envelope', 'rendezvous', 'wrapper')}
    roles['target'] = {'path': 'otelc_witness_provider.py', 'function': 'target'}
    return roles, offsets


class PythonWitnessBootstrapTests(unittest.TestCase):
    def direct_driver(self, mode='normal'):
        # Test-only SDK calls let ctrace measure the import observer with an
        # actual SourceFileLoader stack. Unchanged-source controls remain above.
        functions = {'wrapper': 'return otelc_witness_provider.target(41)',
                     'rendezvous': 'ARM()',
                     'envelope': 'global otelc_witness_provider\nimport otelc_witness_provider\nrendezvous()\nreturn wrapper()'}
        source = 'import sys\nfrom builtins import _otelc_test_monitor as MONITOR, _otelc_test_arm as ARM\n'
        source += 'MONITOR.start(sys._getframe().f_code, 0)\ntry:\n'
        for function, body in functions.items():
            source += f'    def {function}():\n        MONITOR.start(sys._getframe().f_code, 0)\n        try:\n'
            source += ''.join('            ' + line + '\n' for line in body.splitlines())
            source += "        finally:\n            MONITOR.returned(sys._getframe().f_code, 0, None)\n"
        source += "    RESULT = envelope()\nfinally:\n    MONITOR.returned(sys._getframe().f_code, 0, None)\n"
        provider = r'''import sys
from builtins import _otelc_test_monitor as MONITOR, _otelc_test_mode as MODE
loader = sys._getframe().f_back.f_back.f_locals['self']
class Opaque:
    def __eq__(self, other): raise AssertionError('loader metadata equality hook')
    def __str__(self): raise AssertionError('loader metadata string hook')
class Mapping(dict):
    def items(self): raise AssertionError('loader namespace method hook')
if MODE == 'extra': loader.debug = True
if MODE == 'namespace': loader.__dict__ = Mapping(loader.__dict__)
if MODE == 'path': loader.path = Opaque()
if MODE == 'name': loader.name = Opaque()
MONITOR.start(sys._getframe().f_code, 0)
def target(value):
    MONITOR.start(sys._getframe().f_code, 0)
    try:
        if MODE == 'late_source':
            from pathlib import Path
            path = Path(__file__)
            path.write_text(path.read_text() + '# changed after admission\n')
        return value + 1
    finally:
        MONITOR.returned(sys._getframe().f_code, 0, None)
MONITOR.returned(sys._getframe().f_code, 0, None)
'''
        with tempfile.TemporaryDirectory(prefix='otelc-import-api-', dir='/private/tmp') as name:
            folder = Path(name)
            policy, _ = self.prepare(folder, source)
            (folder / 'otelc_witness_provider.py').write_text(provider)
            roles, offsets = declaration(folder, source)
            program = FrozenProgram.freeze(folder, policy, 'harness.py', roles, offsets)
            root_code = compile(source, str(folder / 'harness.py'), 'exec')
            state = program.state()
            traces = TraceCapture()
            runtime = Runtime(policy, Capture(), traces, witness_state=state)
            self.addCleanup(runtime.close)
            bootstrap = WitnessBootstrap(runtime, program, root_code)
            with contextlib.chdir(folder):
                monitor = Monitor(policy, runtime, witness=bootstrap)
                def arm():
                    if state.phase == 'ready':
                        self.assertTrue(state_tests.PythonWitnessStateTests().arm(state)['ok'])
                sys.path.insert(0, str(folder))
                try:
                    with patch.object(builtins, '_otelc_test_monitor', monitor, create=True), \
                         patch.object(builtins, '_otelc_test_arm', arm, create=True), \
                         patch.object(builtins, '_otelc_test_mode', mode, create=True):
                        namespace = {}
                        exec(root_code, namespace)
                    self.assertEqual(42, namespace['RESULT'])
                finally:
                    sys.path.remove(str(folder))
                    sys.modules.pop('otelc_witness_provider', None)
                    bootstrap.close()
            report = runtime.close()
            return [decode_record(frame) for frame in state.drain()], traces, report

    def test_direct_api_imports_measure_actual_loader_frames_and_refuse_metadata_hooks(self):
        for mode in ('normal', 'extra', 'namespace', 'path', 'name', 'late_source'):
            with self.subTest(mode=mode):
                events, traces, _ = self.direct_driver(mode)
                self.assertEqual(mode == 'normal', events[-1]['complete'])
                if mode == 'normal':
                    self.assertEqual(2, len(traces.spans()))

    def prepare(self, folder: Path, source: str):
        (folder / 'harness.py').write_text(source)
        (folder / 'otelc_witness_provider.py').write_text(PROVIDER)
        policy = trace_plan()
        policy['source_matchers']['include'] = [r'(?-u).*\.py$']
        policy['function_matchers']['include'] = [r'(?-u)^(harness\.wrapper|otelc_witness_provider\.target)$']
        roles, offsets = declaration(folder, source)
        program = FrozenProgram.freeze(folder, policy, 'harness.py', roles, offsets)
        return policy, program

    def execute(self, *, lazy=False, control='', preloaded=False, loader_clone=False):
        with tempfile.TemporaryDirectory(prefix='otelc-frozen-witness-', dir='/private/tmp') as name:
            folder = Path(name)
            source = harness(lazy, control, loader_clone)
            policy, program = self.prepare(folder, source)
            original = {path.name: path.read_bytes() for path in folder.glob('*.py')}
            root_code = compile(source, str(folder / 'harness.py'), 'exec')
            sys.path.insert(0, str(folder))
            self.addCleanup(sys.modules.pop, 'otelc_witness_provider', None)
            try:
                if preloaded:
                    with contextlib.redirect_stdout(io.StringIO()):
                        importlib.import_module('otelc_witness_provider')
                release = threading.Event(); release.set()
                baseline = {'RELEASE': release}
                with contextlib.redirect_stdout(io.StringIO()) as baseline_output:
                    exec(root_code, baseline)
                if not preloaded:
                    sys.modules.pop('otelc_witness_provider', None)
                traces = TraceCapture()
                state = program.state()
                self.assertFalse(state.available)
                runtime = Runtime(policy, Capture(), traces, witness_state=state)
                self.addCleanup(runtime.close)
                bootstrap = WitnessBootstrap(runtime, program, root_code)
                self.assertIsNone(bootstrap._code('target'))
                release = threading.Event()
                responses = []

                def consumer():
                    deadline = time.monotonic() + 4
                    while state.phase == 'idle' and state.fault is None and time.monotonic() < deadline:
                        time.sleep(0.001)
                    if state.phase == 'ready':
                        responses.append(state_tests.PythonWitnessStateTests().arm(state))
                    release.set()

                worker = threading.Thread(target=consumer)
                worker.start()
                with contextlib.chdir(folder):
                    monitor = Monitor(policy, runtime, witness=bootstrap)
                    monitor.install()
                    try:
                        namespace = {'RELEASE': release}
                        with contextlib.redirect_stdout(io.StringIO()) as measured_output:
                            exec(root_code, namespace)
                    finally:
                        monitor.close()
                        worker.join(5)
                        self.assertFalse(worker.is_alive())
                report = runtime.close()
                self.assertEqual(baseline_output.getvalue(), measured_output.getvalue())
                self.assertEqual(baseline['RESULT'], namespace['RESULT'])
                self.assertEqual(original, {path.name: path.read_bytes() for path in folder.glob('*.py')})
                return program.record(), state, [decode_record(frame) for frame in state.drain()], traces, report, responses
            finally:
                sys.path.remove(str(folder))
                sys.modules.pop('otelc_witness_provider', None)

    def test_original_top_level_and_lazy_imports_bind_frozen_probe_ids_to_sdk_output(self):
        for lazy in (False, True):
            with self.subTest(lazy=lazy):
                record, state, events, traces, report, responses = self.execute(lazy=lazy)
                self.assertTrue(events[-1]['complete'])
                self.assertEqual(1, len(responses))
                self.assertTrue(responses[0]['ok'])
                self.assertEqual(record['build_id'], state.bindings.build_id)
                self.assertEqual(1, state.target_entries)
                spans = traces.spans()
                self.assertEqual(2, len(spans))
                for span in spans:
                    attributes = {item.key: item.value.string_value for item in span.attributes}
                    role = 'wrapper' if span.name == 'harness.wrapper' else 'target'
                    self.assertEqual(record['roles'][role]['probe_id'], attributes['otelc.probe.id'])
                    self.assertEqual(state.scope_id, attributes['otelc.scope.id'])
                self.assertFalse(report['traces']['losses'])

    def test_preloaded_target_equal_code_clone_and_reload_cannot_satisfy_the_binding(self):
        clone = '''from types import FunctionType
otelc_witness_provider.target = FunctionType(otelc_witness_provider.target.__code__.replace(), otelc_witness_provider.__dict__)
'''
        reload = 'import importlib\nimportlib.reload(otelc_witness_provider)\n'
        for changes in ({'preloaded': True}, {'control': clone}, {'control': reload}):
            with self.subTest(changes=changes):
                _, _, events, _, _, _ = self.execute(**changes)
                self.assertFalse(events[-1]['complete'])
                self.assertTrue(any(event['event'] == 'invalidated' for event in events))

    def test_replaced_standard_loader_compiler_cannot_attest_equal_cloned_code(self):
        _, _, events, _, _, _ = self.execute(loader_clone=True)
        self.assertFalse(events[-1]['complete'])

    def test_a_loader_override_beyond_the_namespace_scan_limit_cannot_hide(self):
        _, _, events, _, _, _ = self.execute(loader_clone='hidden')
        self.assertFalse(events[-1]['complete'])

    def test_manifest_roundtrip_refuses_source_policy_site_and_record_changes(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            folder = Path(name)
            policy, program = self.prepare(folder, harness())
            self.assertEqual(program.bytes(), FrozenProgram.load(program.bytes(), folder, policy).bytes())
            for change in ('build', 'site', 'unknown', 'compiler'):
                record = program.record()
                if change == 'build': record['build_id'] = '0' * 64
                if change == 'site': record['sites']['target']['call_offset'] += 2
                if change == 'unknown': record['unknown'] = True
                if change == 'compiler': record['compiler']['optimization'] += 1
                with self.subTest(change=change), self.assertRaises(CodeBindingError):
                    FrozenProgram.load(json.dumps(record).encode(), folder, policy)
            changed = dict(policy, function_matchers={'include': [], 'exclude': []})
            with self.assertRaises(CodeBindingError):
                FrozenProgram.load(program.bytes(), folder, changed)
            (folder / 'otelc_witness_provider.py').write_text(PROVIDER + '# changed\n')
            with self.assertRaises(CodeBindingError):
                program.validate_sources()
            with self.assertRaises(CodeBindingError):
                FrozenProgram.load(program.bytes(), folder, policy)

    def test_namespace_read_is_bounded_and_does_not_dispatch_application_keys(self):
        class Key:
            def __hash__(self):
                return 0

            def __eq__(self, _other):
                raise AssertionError('application equality was invoked')

        namespace = {Key(): object(), 'name': 'kept'}
        self.assertEqual('kept', _member(namespace, 'name'))
        self.assertIsNone(_member({**{str(index): index for index in range(65)}, 'name': 'hidden'}, 'name'))

    def test_source_inventory_limits_special_files_and_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            folder = Path(name)
            self.prepare(folder, harness())
            for setting, limit in (('MAX_FILES', 1), ('MAX_SOURCE_BYTES', 1), ('MAX_DIRECTORIES', 0)):
                with self.subTest(setting=setting), patch.object(witness_manifest, setting, limit), \
                     self.assertRaisesRegex(CodeBindingError, 'source_inventory_capacity'):
                    witness_manifest.inventory(folder)
            special = folder / 'special.py'
            os.mkfifo(special)
            with self.assertRaisesRegex(CodeBindingError, 'unsupported_source_file'):
                witness_manifest.inventory(folder)
            special.unlink()
            special.symlink_to(folder / 'harness.py')
            with self.assertRaisesRegex(CodeBindingError, 'unsupported_source_file'):
                witness_manifest.inventory(folder)
            special.unlink()
            (folder / 'linked').symlink_to(folder, target_is_directory=True)
            with self.assertRaisesRegex(CodeBindingError, 'symlink_source_directory'):
                witness_manifest.inventory(folder)

    def test_replacement_loader_descriptor_is_rejected_without_calling_it(self):
        class Descriptor:
            def __get__(self, _instance, _owner):
                raise AssertionError('application loader descriptor was invoked')

        with patch.object(SourceFileLoader, 'get_code', Descriptor()):
            self.assertFalse(_qualified_loader())

    def test_non_python_disabled_or_asynchronous_policy_cannot_freeze_this_profile(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            folder = Path(name)
            policy, _ = self.prepare(folder, harness())
            roles, offsets = declaration(folder, harness())
            for change in ({'language': 'java'}, {'execution_available': False},
                           {'traces': {'enabled': False}}, {'propagation': {'tasks': True}},
                           {'lifetimes': {'enabled': True}}):
                with self.subTest(change=change), self.assertRaisesRegex(CodeBindingError, 'unsupported_witness_policy'):
                    FrozenProgram.freeze(folder, dict(policy, **change), 'harness.py', roles, offsets)

    def test_common_selection_existing_annotations_and_exclusion_are_preserved(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            folder = Path(name)
            source = harness().replace('def wrapper():', '# otelc.instrument\ndef wrapper():')
            policy, _ = self.prepare(folder, source)
            policy['annotations']['read_existing'] = True
            policy['function_matchers']['include'] = []
            (folder / 'otelc_witness_provider.py').write_text('# otelc.instrument\ndef target(value):\n    return value + 1\n')
            roles, offsets = declaration(folder, source)
            program = FrozenProgram.freeze(folder, policy, 'harness.py', roles, offsets)
            self.assertFalse(program.state().available)
            policy['function_matchers']['exclude'] = ['(?-u).*target$']
            with self.assertRaisesRegex(CodeBindingError, 'witness_role_not_selected'):
                FrozenProgram.freeze(folder, policy, 'harness.py', roles, offsets)
