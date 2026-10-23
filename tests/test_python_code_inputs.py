"""Native code records identify genuine starts without retaining application code."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from otelc import python_input_journal as journal
from python_input_support import NativeInputCase


ROOT = Path(__file__).resolve().parents[1]
SETUP = 'import sys\nsys.path.insert(0, ' + repr(str(ROOT)) + ')\n' + '''import _otelc_inputs as native
from otelc.python_code_descriptor import encode
identities = []
def observer(code, offset):
    if code.co_name in NAMES and code.co_filename == __file__:
        identities.append(native.observe(code, offset))
assert native.bind(observer, encode) is True
sys.monitoring.use_tool_id(3, 'native-code-test')
sys.monitoring.register_callback(3, sys.monitoring.events.PY_START, observer)
sys.monitoring.set_events(3, sys.monitoring.events.PY_START)
'''
FINISH = '''
sys.monitoring.set_events(3, 0)
sys.monitoring.register_callback(3, sys.monitoring.events.PY_START, None)
sys.monitoring.free_tool_id(3)
'''


class PythonCodeInputTests(NativeInputCase):
    def descriptor(self, value: journal.CodeInput) -> tuple[dict, dict[int, dict]]:
        graph = json.loads(value.content)
        nodes = {node['id']: node for node in graph['nodes']}
        return nodes[graph['root']], nodes

    def constant(self, value: journal.CodeInput, position: int) -> dict:
        root, nodes = self.descriptor(value)
        constants = nodes[root['fields']['co_consts']]
        return nodes[constants['items'][position]]
    def run_code(self, body: str, names: tuple[str, ...], *, setup: str = SETUP
                 ) -> tuple[list, journal.InputJournal]:
        source = 'NAMES=' + repr(names) + '\n' + setup + body + FINISH
        entry = self.root / 'entry.py'
        entry.write_text(source)
        status, out, err, path, pid = self.launch(['-B', str(entry)])
        self.assertEqual((status, err), (0, b''), out)
        rows = [json.loads(line) for line in out.splitlines()]
        return rows, journal.read(path, expected_pid=pid)

    def test_real_code_bytes_and_identity_are_bound_to_start_frames(self) -> None:
        rows, evidence = self.run_code('''
def workload(value):
    return value + 701
assert workload(1) == 702 and workload(2) == 703
import json
print(json.dumps(identities))
''', ('workload',))
        self.assertEqual(rows, [[1, 1]])
        self.assertTrue(evidence.complete)
        self.assertEqual(len(evidence.codes), 1)
        saved = evidence.codes[0]
        self.assertEqual(saved.identity, 1)
        self.assertEqual(saved.python_version, sys.hexversion)
        self.assertEqual(saved.descriptor_version, 2)
        root, nodes = self.descriptor(saved)
        self.assertEqual(nodes[root['fields']['co_name']]['value'], 'workload')
        value = self.constant(saved, -1)
        self.assertEqual(value['type'], 'integer')
        self.assertEqual(int.from_bytes(bytes.fromhex(value['data']), signed=True), 701)

    def test_closures_generators_coroutines_and_constant_shapes_are_observed(self) -> None:
        rows, evidence = self.run_code('''
import asyncio, json
def factory(value):
    def nested(): return value + 1
    return nested
def generator(): yield 7
async def coroutine():
    await asyncio.sleep(0)
    return 7
def constants(): return (None, True, False, ..., 2**400, 1.5, 2j, 'text', b'bytes', frozenset({1,2}))
assert factory(6)() == 7 and next(generator()) == 7
assert asyncio.run(coroutine()) == 7
assert constants()[4] == 2**400
print(json.dumps(identities))
''', ('factory', 'nested', 'generator', 'coroutine', 'constants'))
        self.assertEqual(rows, [[1, 2, 3, 4, 5]])
        self.assertTrue(evidence.complete)
        self.assertEqual(len(evidence.codes), 5)
        self.assertEqual({nodes[root['fields']['co_name']]['value']
                          for root, nodes in map(self.descriptor, evidence.codes)},
                         {'factory', 'nested', 'generator', 'coroutine', 'constants'})

    def test_direct_callback_audit_and_wrong_frame_cannot_invent_code_records(self) -> None:
        rows, evidence = self.run_code('''
import json
def workload():
    observer(workload.__code__, 0)
    assert native.observe(workload.__code__, 0) is None
sys.audit('exec', workload.__code__)
workload()
print(json.dumps(identities))
''', ('workload',))
        self.assertEqual(rows, [[1, None]])
        self.assertFalse(evidence.complete)
        self.assertGreaterEqual(evidence.capture_failures, 2)
        self.assertEqual(len(evidence.codes), 1)

    def test_replaced_code_and_collected_identity_never_reuse_an_old_record(self) -> None:
        rows, evidence = self.run_code('''
import gc, json, types, weakref
def workload(): return 701
assert workload() == 701
workload.__code__ = workload.__code__.replace(co_consts=tuple(
    702 if type(value) is int and value == 701 else value for value in workload.__code__.co_consts))
assert workload() == 702
def temporary(): return 703
function = types.FunctionType(temporary.__code__.replace(), {})
reference = weakref.ref(function.__code__)
assert function() == 703
del function
assert reference() is None
gc.collect()
assert reference() is None
print(json.dumps(identities))
''', ('workload', 'temporary'))
        self.assertEqual(rows, [[1, 2, 3]])
        self.assertTrue(evidence.complete)
        self.assertEqual([int.from_bytes(bytes.fromhex(self.constant(value, -1)['data']), signed=True)
                          for value in evidence.codes], [701, 702, 703])

    def test_unsupported_constants_never_invoke_application_hooks_or_claim_success(self) -> None:
        rows, evidence = self.run_code('''
import json
class Trap(bytearray):
    def __buffer__(self, flags): raise AssertionError('buffer callback')
    def __repr__(self): raise AssertionError('repr callback')
    def __str__(self): raise AssertionError('str callback')
trap = Trap(b'x')
def workload(): return 701
workload.__code__ = workload.__code__.replace(co_consts=tuple(
    trap if type(value) is int and value == 701 else value for value in workload.__code__.co_consts))
assert workload() is trap and workload() is trap
print(json.dumps(identities))
''', ('workload',))
        self.assertEqual(rows, [[None, None]])
        self.assertFalse(evidence.complete)
        self.assertEqual(evidence.codes, ())
        self.assertEqual(evidence.capture_failures, 1)

    def test_payload_and_depth_limits_preserve_original_application_results(self) -> None:
        rows, evidence = self.run_code('''
import json
def workload(): return 701
large = b'x' * (16 * 1024 * 1024)
workload.__code__ = workload.__code__.replace(co_consts=tuple(
    large if type(value) is int and value == 701 else value for value in workload.__code__.co_consts))
assert workload() is large
deep = None
for _ in range(150): deep = (deep,)
workload.__code__ = workload.__code__.replace(co_consts=tuple(
    deep if value is large else value for value in workload.__code__.co_consts))
assert workload() is deep
print(json.dumps(identities))
''', ('workload',))
        self.assertEqual(rows, [[None, None]])
        self.assertFalse(evidence.complete)
        self.assertEqual(evidence.codes, ())

    def test_invalid_binding_and_offsets_are_explicit_capture_failure(self) -> None:
        rows, evidence = self.run_code('''
import json
def workload(): return 7
assert native.bind(object(), encode) is None
assert native.bind(lambda: None, encode) is None
for position in (True, -1, 2**100, 'zero'):
    assert native.observe(workload.__code__, position) is None
assert native.observe(None, 0) is None
assert workload() == 7
print(json.dumps(identities))
''', ('workload',))
        self.assertEqual(rows, [[1]])
        self.assertFalse(evidence.complete)
        self.assertGreaterEqual(evidence.capture_failures, 7)

    def test_code_observation_adds_no_application_marshalling_audit_callbacks(self) -> None:
        rows, evidence = self.run_code('''
import json
audits = []
def audit(name, values):
    if name in ('marshal.dumps', 'builtins.id'): audits.append(name)
sys.addaudithook(audit)
def workload(): return 7
assert workload() == 7
print(json.dumps(audits))
''', ('workload',))
        self.assertEqual(rows, [[]])
        self.assertTrue(evidence.complete)

    def test_threads_share_pending_identity_and_accept_out_of_order_completion(self) -> None:
        setup = SETUP.replace('assert native.bind(observer, encode) is True', """
import threading
entered = threading.Event()
release = threading.Event()
def prepare(code, *, _pinned=True):
    if code.co_name == 'slow':
        entered.set()
        assert release.wait(5)
    return encode(code)
assert native.bind(observer, prepare)
""")
        rows, evidence = self.run_code('''
import json
def slow(): return 701
def fast(): return 702
worker = threading.Thread(target=slow)
worker.start()
assert entered.wait(5)
assert slow() == 701
assert fast() == 702
release.set()
worker.join(5)
assert not worker.is_alive()
print(json.dumps(sorted(identities)))
''', ('slow', 'fast'), setup=setup)
        self.assertEqual(rows, [[1, 1, 2]])
        self.assertTrue(evidence.complete)
        self.assertEqual([code.identity for code in evidence.codes], [2, 1])

    def test_failed_pending_descriptor_cannot_become_complete(self) -> None:
        setup = SETUP.replace('assert native.bind(observer, encode) is True', """
import threading
entered = threading.Event()
release = threading.Event()
def prepare(code, *, _pinned=True):
    entered.set()
    assert release.wait(5)
    raise ValueError('deliberate descriptor failure')
assert native.bind(observer, prepare)
""")
        rows, evidence = self.run_code('''
import json
def slow(): return 701
worker = threading.Thread(target=slow)
worker.start()
assert entered.wait(5)
assert slow() == 701
release.set()
worker.join(5)
assert not worker.is_alive()
assert slow() == 701
print(json.dumps(sorted((value for value in identities if value is not None))))
''', ('slow',), setup=setup)
        self.assertEqual(rows, [[1]])
        self.assertFalse(evidence.complete)
        self.assertEqual(evidence.codes, ())
        self.assertEqual(evidence.capture_failures, 1)

    def test_encoder_mutation_and_invalid_payload_cannot_claim_success(self) -> None:
        for replacement in ("return 'wrong type'", "raise MemoryError('descriptor')"):
            setup = SETUP.replace('assert native.bind(observer, encode) is True',
                'def prepare(code, *, _pinned=True):\n    ' + replacement
                + '\nassert native.bind(observer, prepare)')
            with self.subTest(replacement=replacement):
                rows, evidence = self.run_code('''
import json
def workload(): return 7
assert workload() == 7
print(json.dumps(identities))
''', ('workload',), setup=setup)
                self.assertEqual(rows, [[None]])
                self.assertFalse(evidence.complete)
        rows, evidence = self.run_code('''
import json
encode.__kwdefaults__['_quote'] = lambda value: 'spoofed'
def workload(): return 7
assert workload() == 7
print(json.dumps(identities))
''', ('workload',))
        self.assertEqual(rows, [[None]])
        self.assertFalse(evidence.complete)
        self.assertEqual(evidence.codes, ())

    def test_embedded_nul_filenames_are_not_silently_truncated(self) -> None:
        rows, evidence = self.run_code('''
import json
def workload(): return 701
original = workload.__code__
base = __file__
for filename in (base + chr(0) + 'suffix', chr(0) + base, ''):
    __file__ = filename
    workload.__code__ = original.replace(co_filename=filename)
    assert workload() == 701
print(json.dumps(identities))
''', ('workload',))
        self.assertEqual(rows, [[None, None, None]])
        self.assertFalse(evidence.complete)
        self.assertEqual(evidence.codes, ())
        self.assertEqual(evidence.capture_failures, 3)

    def test_surrogateescaped_filename_bytes_agree_with_the_code_descriptor(self) -> None:
        rows, evidence = self.run_code('''
import json
def workload(): return 701
__file__ += chr(0xdcff)
workload.__code__ = workload.__code__.replace(co_filename=__file__)
assert workload() == 701
print(json.dumps(identities))
''', ('workload',))
        self.assertEqual(rows, [[1]])
        self.assertTrue(evidence.complete)
        saved = evidence.codes[0]
        root, nodes = self.descriptor(saved)
        import os
        self.assertEqual(os.fsencode(nodes[root['fields']['co_filename']]['value']), saved.path)
        self.assertTrue(saved.path.endswith(b'\xff'))

    def test_descriptor_records_the_actual_application_filesystem_codec(self) -> None:
        rows, evidence = self.run_code('''
import json
def workload(): return 701
assert workload() == 701
print(json.dumps({'encoding': sys.getfilesystemencoding(), 'errors': sys.getfilesystemencodeerrors()}))
''', ('workload',))
        self.assertTrue(evidence.complete)
        self.assertEqual(evidence.codes[0].descriptor_version, 2)
        data = json.loads(evidence.codes[0].content)
        self.assertEqual(data['schema_version'], 2)
        self.assertEqual(data['filesystem'], rows[0])
