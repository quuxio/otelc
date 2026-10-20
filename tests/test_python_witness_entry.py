"""Qualify deferred profiles over real imports without preloading providers."""
import dis
from importlib import _bootstrap
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys
import tempfile
from types import CodeType
import unittest

from test_python_adapter import ROOT
import test_python_witness_owner as owner_tests
from quux_otelc_python.witness_callsite import bind_site
from quux_otelc_python.witness_code import CodeBindingError, CodeRegistry
from quux_otelc_python.witness_entry import EntryAnchor
from quux_otelc_python.witness_owner import ExecutionOwner, OwnerProfile


PROVIDER = 'def target(value):\n    return value + 1\n'


def harness(lazy):
    initial = '' if lazy else 'import otelc_witness_provider\n'
    deferred = '    global otelc_witness_provider\n    import otelc_witness_provider\n' if lazy else ''
    return initial + '''def rendezvous():
    return None
def wrapper():
    return otelc_witness_provider.target(41)
def envelope():
''' + deferred + '''    rendezvous()
    return wrapper()
RESULT = envelope()
'''


class PythonWitnessEntryTests(unittest.TestCase):
    def test_direct_api_drivers_cover_live_transfer_and_original_unwind(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                namespace, prepared = owner_tests.PythonWitnessOwnerTests().unit_driver(fail=fail)
                registry = CodeRegistry()
                root = namespace['primary'].__code__
                envelope = namespace['envelope'].__code__
                registry.register(root); registry.register(envelope)
                anchor = EntryAnchor(registry, root, prepared.profile.envelope)

                class Deferred:
                    def __init__(self):
                        self.owner = None
                        self.adopted = None

                    def entered(self, frame):
                        if self.owner is None:
                            event = anchor.entered(frame)
                            if frame.f_code is prepared.profile.rendezvous.callee():
                                self.owner = ExecutionOwner(prepared.profile)
                                self.adopted = self.owner.adopt_entry(anchor, frame)
                                with test.assertRaisesRegex(CodeBindingError, 'owner_already_started'):
                                    self.owner.adopt_entry(anchor, frame)
                                with test.assertRaisesRegex(CodeBindingError, 'entry_not_live'):
                                    ExecutionOwner(prepared.profile).adopt_entry(anchor, frame)
                                return self.owner.entered(frame)
                            return event
                        return self.owner.entered(frame)

                    def exited(self, frame, outcome):
                        return self.owner.exited(frame, outcome) if self.owner is not None else anchor.exited(frame)

                test = self
                deferred = Deferred()
                namespace['OWNER'] = deferred
                if fail:
                    with self.assertRaises(ValueError) as caught:
                        namespace['primary']()
                    self.assertIs(caught.exception, namespace['ERROR'])
                    self.assertIsNone(caught.exception.__context__)
                else:
                    self.assertEqual(namespace['primary'](), 42)
                self.assertEqual(deferred.adopted.kind, 'entry_adopted')
                self.assertTrue(anchor.claimed)
                self.assertIsNone(anchor.primary)
                self.assertIsNone(anchor.envelope)
                self.assertIsNone(anchor.close())
                self.assertIsNone(deferred.owner.fault)
                self.assertIsNone(deferred.owner.close())

    def test_direct_api_entry_without_adoption_retires_and_rejects_replay(self):
        namespace, prepared = owner_tests.PythonWitnessOwnerTests().unit_driver()
        registry = CodeRegistry()
        root, envelope = namespace['primary'].__code__, namespace['envelope'].__code__
        registry.register(root); registry.register(envelope)
        anchor = EntryAnchor(registry, root, prepared.profile.envelope)

        class Unclaimed:
            def entered(self, frame):
                return anchor.entered(frame)

            def exited(self, frame, _outcome):
                return anchor.exited(frame)

        namespace['OWNER'] = Unclaimed()
        self.assertEqual(namespace['primary'](), 42)
        self.assertIsNone(anchor.fault)
        self.assertIsNone(anchor.primary)
        self.assertIsNone(anchor.envelope)
        self.assertEqual(namespace['primary'](), 42)
        self.assertEqual(anchor.fault, 'primary_reentered')
        self.assertIsNone(anchor.entered(object()))
        self.assertIsNone(anchor.exited(object()))
        self.assertIsNone(anchor.close())

    def execute(self, lazy=False, *, repeated=False, close_early=False, prelude=False):
        with tempfile.TemporaryDirectory(prefix='otelc-witness-entry-', dir='/private/tmp') as name:
            folder = Path(name)
            provider = folder / 'otelc_witness_provider.py'; provider.write_text(PROVIDER)
            source = harness(lazy)
            if prelude:
                source = source.replace('RESULT = envelope()', 'rendezvous()\nRESULT = envelope()')
            script = folder / 'original.py'; script.write_text(source)
            original = {path: path.read_bytes() for path in (provider, script)}
            root = compile(script.read_bytes(), str(script), 'exec')
            codes = {code.co_name: code for code in root.co_consts if type(code) is CodeType}
            codes['primary'] = root
            registry = CodeRegistry()
            for code in codes.values():
                registry.register(code)

            def site(caller, callee, index=-1):
                offsets = [instruction.offset for instruction in dis.get_instructions(codes[caller])
                           if instruction.opname == 'CALL']
                return bind_site(registry, codes[caller], codes[callee], offsets[index])

            entry = EntryAnchor(registry, root, site('primary', 'envelope'))
            owner = None
            events, modules, refusals = [], [], []
            loader_code = SourceFileLoader.exec_module.__code__
            bridge_code = _bootstrap._call_with_frames_removed.__code__

            def entered(code, _offset):
                nonlocal owner
                frame = sys._getframe(1)
                if owner is not None:
                    event = owner.entered(frame)
                    if event is not None:
                        events.append(event)
                    return
                event = entry.entered(frame)
                if event is not None:
                    events.append(event)
                if code.co_name == '<module>' and code.co_filename == str(provider):
                    caller = frame.f_back
                    loader_frame = caller.f_back if caller is not None and caller.f_code is bridge_code else caller
                    self.assertIsNotNone(loader_frame)
                    self.assertIs(loader_frame.f_code, loader_code)
                    self.assertIs(loader_frame.f_locals['code'], code)
                    self.assertIs(frame.f_globals, loader_frame.f_locals['module'].__dict__)
                    modules.append(id(code))
                    for value in code.co_consts:
                        if type(value) is CodeType and value.co_name == 'target':
                            codes['target'] = value
                            registry.register(value)
                if code is codes['rendezvous'] and 'target' in codes:
                    profile = OwnerProfile(registry, root, site('primary', 'envelope'),
                                           site('envelope', 'rendezvous', -2), site('envelope', 'wrapper'),
                                           site('wrapper', 'target'))
                    candidate = ExecutionOwner(profile)
                    if close_early:
                        entry.close()
                    try:
                        events.append(candidate.adopt_entry(entry, frame))
                    except CodeBindingError as error:
                        refusals.append(str(error))
                    else:
                        owner = candidate
                        events.append(owner.entered(frame))

            def exited(_code, _offset, _value):
                frame = sys._getframe(1)
                event = owner.exited(frame, 'return') if owner is not None else entry.exited(frame)
                if event is not None:
                    events.append(event)

            sys.path.insert(0, str(folder))
            self.assertNotIn('otelc_witness_provider', sys.modules)
            tool = next(number for number in (3, 4, 2, 5) if sys.monitoring.get_tool(number) is None)
            sys.monitoring.use_tool_id(tool, 'otelc-private-entry-test')
            try:
                sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, entered)
                sys.monitoring.register_callback(tool, sys.monitoring.events.PY_RETURN, exited)
                sys.monitoring.set_events(tool, sys.monitoring.events.PY_START | sys.monitoring.events.PY_RETURN)
                namespace = {}
                exec(root, namespace)
                self.assertEqual(namespace['RESULT'], 42)
                if repeated:
                    exec(root, {})
            finally:
                sys.monitoring.set_events(tool, 0)
                sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, None)
                sys.monitoring.register_callback(tool, sys.monitoring.events.PY_RETURN, None)
                sys.monitoring.free_tool_id(tool)
                sys.path.remove(str(folder))
                sys.modules.pop('otelc_witness_provider', None)
            self.assertEqual({path: path.read_bytes() for path in original}, original)
            self.assertEqual(len(modules), 1)
            return entry, owner, events, refusals

    def test_original_module_and_lazy_import_bind_after_actual_primary_and_envelope_entry(self):
        self.assertTrue(ROOT.is_dir())
        for lazy in (False, True):
            with self.subTest(lazy=lazy):
                entry, owner, events, refusals = self.execute(lazy)
                self.assertFalse(refusals)
                self.assertTrue(entry.claimed)
                self.assertIsNone(entry.fault)
                self.assertIsNone(owner.fault)
                self.assertEqual([event.kind for event in events],
                                 ['primary_enter', 'envelope_enter', 'entry_adopted', 'rendezvous',
                                  'wrapper_enter', 'target_enter', 'target_exit', 'wrapper_exit',
                                  'envelope_exit', 'primary_exit'])
                context = events[1].context
                self.assertTrue(all(event.context is context for event in events[1:-1]))
                self.assertIs(events[0].activation, events[-1].activation)
                self.assertEqual(owner.inflight, 0)
                self.assertIsNone(owner.close())

    def test_repeated_primary_cannot_get_a_new_generation_after_transfer(self):
        _, owner, events, _ = self.execute(repeated=True)
        self.assertEqual(owner.fault, 'primary_reentered')
        self.assertEqual(sum(event.kind == 'entry_adopted' for event in events), 1)
        self.assertEqual(sum(event.kind == 'invalidated' for event in events), 1)

    def test_closed_entry_refuses_adoption_and_leaves_the_original_application_result(self):
        entry, owner, events, refusals = self.execute(close_early=True)
        self.assertIsNone(owner)
        self.assertEqual(entry.fault, 'incomplete_entry')
        self.assertEqual(refusals, ['entry_not_live'])
        self.assertFalse(any(event.kind == 'entry_adopted' for event in events))

    def test_unrelated_rendezvous_before_envelope_does_not_consume_the_real_entry(self):
        entry, owner, events, refusals = self.execute(prelude=True)
        self.assertEqual(refusals, ['entry_not_live'])
        self.assertTrue(entry.claimed)
        self.assertIsNone(owner.fault)
        self.assertEqual(sum(event.kind == 'rendezvous' for event in events), 1)
        self.assertEqual(sum(event.kind == 'entry_adopted' for event in events), 1)
        self.assertIsNone(owner.close())
