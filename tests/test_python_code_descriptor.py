"""Code descriptors preserve immutable graph identity without retaining inputs."""

from __future__ import annotations

import json
import struct
import unittest
import weakref

from otelc.python_code_descriptor import encode


class PythonCodeDescriptorTests(unittest.TestCase):
    def test_values_aliases_binary_floats_and_unicode_are_lossless(self) -> None:
        shared = (b'bytes', 'unicode\udcff', -2**300)
        values = (None, True, False, ..., 0, -1, 1.5, -0.0, 2j,
                  shared, shared, frozenset({701, 702}), slice(1, None, -1))
        original = (lambda: None).__code__
        code = original.replace(co_consts=values)
        graph = json.loads(encode(code))
        nodes = {node['id']: node for node in graph['nodes']}
        root = nodes[graph['root']]
        self.assertEqual(root['type'], 'code')
        constants = nodes[root['fields']['co_consts']]['items']
        self.assertEqual(constants[9], constants[10])
        self.assertEqual(nodes[constants[0]], {'id': constants[0], 'type': 'literal', 'value': None})
        self.assertIs(nodes[constants[1]]['value'], True)
        self.assertIs(nodes[constants[2]]['value'], False)
        self.assertEqual(nodes[constants[3]]['type'], 'ellipsis')
        self.assertEqual(bytes.fromhex(nodes[constants[7]]['data']), struct.pack('!d', -0.0))
        self.assertEqual(bytes.fromhex(nodes[constants[8]]['data']), struct.pack('!dd', 0., 2.))
        items = nodes[constants[9]]['items']
        self.assertEqual(nodes[items[0]]['data'], b'bytes'.hex())
        self.assertEqual(nodes[items[1]]['value'], 'unicode\udcff')
        self.assertEqual(int.from_bytes(bytes.fromhex(nodes[items[2]]['data']), signed=True), -2**300)
        self.assertEqual(nodes[constants[11]]['type'], 'frozenset')
        self.assertEqual(nodes[constants[12]]['type'], 'slice')
        self.assertEqual(code.co_consts, values)

    def test_exception_releases_code_and_constants_without_gc_or_formatters(self) -> None:
        class Trap:
            def __repr__(self):
                raise AssertionError('application repr')

            def __hash__(self):
                raise AssertionError('application hash')

        value = Trap()
        code = (lambda: None).__code__.replace(co_consts=(value,))
        code_ref, value_ref = weakref.ref(code), weakref.ref(value)
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            encode(code)
        del code, value
        self.assertIsNone(code_ref())
        self.assertIsNone(value_ref())

    def test_depth_bound_is_enforced_before_unbounded_recursion(self) -> None:
        value = None
        for _ in range(150):
            value = (value,)
        with self.assertRaisesRegex(ValueError, 'depth limit'):
            encode((lambda: None).__code__.replace(co_consts=(value,)))

    def test_default_json_handler_refuses_values_instead_of_formatting(self) -> None:
        # A controlled C encoder boundary exercises the otherwise unreachable
        # fallback, without exposing an application value to the real encoder.
        def factory(markers, default, *args):
            return lambda *args: default(object())

        with self.assertRaisesRegex(TypeError, 'non-JSON'):
            encode((lambda: None).__code__, _factory=factory)
