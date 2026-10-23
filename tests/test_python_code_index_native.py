"""Read actual native descriptors independently of the application encoder."""

from __future__ import annotations

import json
import sys
from types import CodeType

from otelc import python_code_index as index
from otelc import python_input_journal as journal
from python_input_support import NativeInputCase
from test_python_code_inputs import FINISH, SETUP


class PythonCodeIndexNativeTests(NativeInputCase):
    def test_native_closure_generator_coroutine_and_constants_match_compiled_metadata(self) -> None:
        names = ('workload', 'nested', 'generator', 'coroutine')
        source = 'NAMES=' + repr(names) + '\n' + SETUP + '''
import asyncio, json
def workload(value):
    def nested(): return value + 701
    return nested
def generator(): yield 701
async def coroutine(): return 701
assert workload(1)() == 702
assert next(generator()) == 701
assert asyncio.run(coroutine()) == 701
print(json.dumps(identities))
''' + FINISH
        entry = self.root / 'entry.py'
        entry.write_text(source)
        status, out, err, path, pid = self.launch(['-B', str(entry)])
        self.assertEqual((status, err), (0, b''), out)
        retained = path.read_bytes()
        evidence = journal.read(path, expected_pid=pid)
        self.assertTrue(evidence.complete)
        self.assertEqual(json.loads(out), [1, 2, 3, 4])
        module = compile(source, str(entry), 'exec', dont_inherit=True)

        def codes(code: CodeType):
            yield code
            for child in code.co_consts:
                if type(child) is CodeType:
                    yield from codes(child)

        expected = {code.co_name: code for code in codes(module) if code.co_name in names}
        observed = set()
        for record in evidence.codes:
            decoded = index.decode(record)
            node = decoded.nodes[decoded.root - 1]
            fields = dict(node.fields)
            name = decoded.nodes[fields['co_name'] - 1].value
            observed.add(name)
            original = expected[name]
            self.assertEqual(dict(node.metadata), {name: getattr(original, name) for name in index.METADATA})
            for name in index.BYTE_FIELDS:
                self.assertEqual(decoded.nodes[fields[name] - 1].value, getattr(original, name))
            for name in index.NAME_FIELDS:
                values = decoded.nodes[fields[name] - 1].references
                self.assertEqual(tuple(decoded.nodes[item - 1].value for item in values), getattr(original, name))
            self.assertEqual(record.python_version, sys.hexversion)
        self.assertEqual(observed, set(names))

        self.assertEqual(path.read_bytes(), retained)
