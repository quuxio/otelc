"""A separate descriptor reader rejects malformed data without evaluating code."""

from __future__ import annotations

import dataclasses
import json
import os
import struct
import sys
import unittest
import weakref
from unittest import mock

from otelc.python_input_journal import CodeInput
from otelc import python_code_index as index


def graph() -> dict:
    """A literal wire fixture independent of the application-side encoder."""
    return {'schema_version': 2, 'filesystem': {'encoding': 'utf-8', 'errors': 'surrogateescape'}, 'root': 1, 'nodes': [
        {'id': 1, 'type': 'code', 'metadata': {
            'co_argcount': 0, 'co_posonlyargcount': 0, 'co_kwonlyargcount': 0,
            'co_nlocals': 0, 'co_stacksize': 1, 'co_flags': 3, 'co_firstlineno': 1},
         'fields': {'co_consts': 2, 'co_names': 4, 'co_varnames': 4,
                    'co_cellvars': 4, 'co_freevars': 4, 'co_filename': 5,
                    'co_name': 6, 'co_qualname': 6, 'co_linetable': 7,
                    'co_exceptiontable': 7, 'co_code': 8}},
        {'id': 2, 'type': 'tuple', 'items': [3]},
        {'id': 3, 'type': 'literal', 'value': None},
        {'id': 4, 'type': 'tuple', 'items': []},
        {'id': 5, 'type': 'unicode', 'value': '/source/app.py'},
        {'id': 6, 'type': 'unicode', 'value': 'workload'},
        {'id': 7, 'type': 'bytes', 'data': ''},
        {'id': 8, 'type': 'bytes', 'data': '970064005300'},
    ]}


def record(data: dict | bytes | None = None, **changes) -> CodeInput:
    content = json.dumps(graph() if data is None else data).encode() if type(data) is not bytes else data
    return dataclasses.replace(CodeInput(b'/source/app.py', content, 1, 0, sys.hexversion, 2), **changes)


class PythonCodeIndexTests(unittest.TestCase):
    def test_literal_wire_fixture_becomes_immutable_data_with_exact_aliases(self) -> None:
        value = record()
        result = index.decode(value)
        self.assertEqual((result.identity, result.sha256, result.root), (1, value.sha256, 1))
        self.assertEqual(len(result.nodes), 8)
        self.assertEqual((result.python_version, result.descriptor_version), (sys.hexversion, 2))
        self.assertEqual((result.path, result.offset), (value.path, value.offset))
        self.assertEqual((result.filesystem_encoding, result.filesystem_errors), ('utf-8', 'surrogateescape'))
        fields = dict(result.nodes[0].fields)
        self.assertEqual(fields['co_name'], fields['co_qualname'])
        self.assertEqual(fields['co_names'], fields['co_varnames'])
        self.assertIsNone(result.nodes[2].value)
        self.assertEqual(result.nodes[7].value, bytes.fromhex('970064005300'))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.nodes[0].kind = 'changed'
        with self.assertRaises(TypeError):
            result.nodes[0].fields[0] = ('changed', 99)

    def test_forward_name_tuple_references_are_validated_before_code_shape(self) -> None:
        for field in index.NAME_FIELDS:
            with self.subTest(field=field):
                value = graph()
                value['nodes'].append({'id': 9, 'type': 'tuple', 'items': [999]})
                value['nodes'][0]['fields'][field] = 9
                prepared_input = record(value)
                with self.assertRaisesRegex(ValueError, 'missing node'):
                    index.decode(prepared_input)

    def test_all_constant_types_preserve_bits_and_shared_references(self) -> None:
        data = graph()
        additions = [
            {'type': 'literal', 'value': True}, {'type': 'literal', 'value': False},
            {'type': 'ellipsis'}, {'type': 'integer', 'data': 'ff80'},
            {'type': 'float', 'data': struct.pack('!d', -0.).hex()},
            {'type': 'complex', 'data': struct.pack('!dd', 0., 2.).hex()},
            {'type': 'frozenset', 'items': [3]}, {'type': 'slice', 'items': [3, 3, 3]}]
        for identity, node in enumerate(additions, 9):
            data['nodes'].append({'id': identity, **node})
            data['nodes'][1]['items'].append(identity)
        result = index.decode(record(data))
        self.assertEqual(result.nodes[11].value, bytes.fromhex('ff80'))
        self.assertEqual(result.nodes[12].value, struct.pack('!d', -0.))
        self.assertEqual(result.nodes[15].references, (3, 3, 3))

    def test_reader_does_not_invoke_encoder_imports_compile_or_marshal(self) -> None:
        value = record()
        with mock.patch('builtins.compile', side_effect=AssertionError('compile')),\
             mock.patch('builtins.__import__', side_effect=AssertionError('import')):
            self.assertEqual(index.decode(value).identity, 1)

    def test_completed_graphs_are_released_without_waiting_for_cyclic_gc(self) -> None:
        references = []
        original = index.Node

        def node(*args, **kwargs):
            value = original(*args, **kwargs)
            references.append(weakref.ref(value))
            return value

        with mock.patch.object(index, 'Node', side_effect=node):
            result = index.decode(record())
            self.assertTrue(all(reference() is not None for reference in references))
            del result
            self.assertTrue(all(reference() is None for reference in references))

    def test_header_types_sizes_versions_and_entry_offsets_are_closed(self) -> None:
        cases = ({'content': b''}, {'content': bytearray(b'{}')}, {'path': b''},
                 {'path': b'x\0y'}, {'path': 'text'}, {'identity': True}, {'identity': 0},
                 {'identity': 2**64}, {'offset': True}, {'offset': -1}, {'offset': 1},
                 {'offset': 6}, {'offset': index.MAX_SOURCE}, {'descriptor_version': True},
                 {'descriptor_version': 1}, {'descriptor_version': 3}, {'python_version': True},
                 {'python_version': 0x030b00f0}, {'python_version': 2**32})
        for change in cases:
            with self.subTest(change=change):
                prepared_input = record(**change)
                with self.assertRaises(ValueError):
                    index.decode(prepared_input)
        with self.assertRaises(ValueError):
            index.decode(object())
        with mock.patch.object(index, 'MAX_SOURCE', 8):
            prepared_input = record()
            with self.assertRaises(ValueError):
                index.decode(prepared_input)
        with mock.patch.object(index, 'MAX_PATH', 8):
            prepared_input = record()
            with self.assertRaises(ValueError):
                index.decode(prepared_input)

    def test_json_duplicate_keys_float_numbers_encoding_and_recursion_refuse(self) -> None:
        for payload in (b'{', b'{}{}', b'[]', b'null', b'{"schema_version":1,"schema_version":1}',
                        b'{"schema_version":1.0}', b'{"x":NaN}', b'{"x":Infinity}',
                        b'{"x":123456789012}', b'\xff', b'"\xc3\xa9"', b'[' * 1500 + b']' * 1500):
            with self.subTest(payload=payload[:60]):
                prepared_input = record(payload)
                with self.assertRaises(ValueError):
                    index.decode(prepared_input)

    def test_schema_identity_reference_and_unknown_fields_refuse(self) -> None:
        cases = []
        for key, value in (('schema_version', True), ('schema_version', 1),
                           ('root', True), ('root', 0), ('root', 3), ('root', 99),
                           ('nodes', []), ('nodes', {})):
            changed = graph()
            changed[key] = value
            cases.append(changed)
        changed = graph();changed['unknown'] = 1;cases.append(changed)
        changed = graph();changed['nodes'][0]['id'] = True;cases.append(changed)
        changed = graph();changed['nodes'][0]['id'] = 2;cases.append(changed)
        changed = graph();changed['nodes'][0]['type'] = 'unknown';cases.append(changed)
        changed = graph();changed['nodes'][1]['items'] = [True];cases.append(changed)
        changed = graph();changed['nodes'][1]['items'] = [999];cases.append(changed)
        for data in cases:
            with self.subTest(data=data):
                prepared_input = record(data)
                with self.assertRaises(ValueError):
                    index.decode(prepared_input)

    def test_code_metadata_and_field_shapes_are_checked_independently(self) -> None:
        for key, value in (('co_flags', -1), ('co_flags', 2**31),
                           ('co_firstlineno', True), ('co_nlocals', 1)):
            data = graph();data['nodes'][0]['metadata'][key] = value
            with self.subTest(key=key, value=value):
                prepared_input = record(data)
                with self.assertRaises(ValueError):
                    index.decode(prepared_input)
        for field, identity in (('co_consts', 3), ('co_filename', 4),
                                ('co_code', 6), ('co_names', 2)):
            data = graph();data['nodes'][0]['fields'][field] = identity
            with self.subTest(field=field):
                prepared_input = record(data)
                with self.assertRaises(ValueError):
                    index.decode(prepared_input)
        for key in ('fields', 'metadata'):
            data = graph();data['nodes'][0][key] = {}
            with self.subTest(key=key):
                prepared_input = record(data)
                with self.assertRaises(ValueError):
                    index.decode(prepared_input)
        data = graph();data['nodes'][7]['data'] = '97'
        prepared_input = record(data)
        with self.assertRaisesRegex(ValueError, 'bytecode width'):
            index.decode(prepared_input)

    def test_binary_literals_and_container_shapes_refuse_noncanonical_data(self) -> None:
        for node in ({'type': 'literal', 'value': 1}, {'type': 'unicode', 'value': 1},
                     {'type': 'ellipsis', 'value': None}, {'type': 'bytes', 'data': '0'},
                     {'type': 'bytes', 'data': 'FF'}, {'type': 'bytes', 'data': 'xx'},
                     {'type': 'integer', 'data': ''}, {'type': 'integer', 'data': '0000'},
                     {'type': 'float', 'data': '00'}, {'type': 'complex', 'data': '00'},
                     {'type': 'tuple', 'items': {}}, {'type': 'slice', 'items': [3]},
                     {'type': 'frozenset', 'items': [3, 3]}, {'type': 'bytes'}):
            data = graph();data['nodes'][1]['items'].append(9)
            data['nodes'].append({'id': 9, **node})
            with self.subTest(node=node):
                prepared_input = record(data)
                with self.assertRaises(ValueError):
                    index.decode(prepared_input)

    def test_cycles_unreachable_nodes_and_all_graph_budgets_refuse(self) -> None:
        data = graph();data['nodes'][1]['items'] = [1]
        prepared_input = record(data)
        with self.assertRaisesRegex(ValueError, 'cycle'):
            index.decode(prepared_input)
        data = graph();data['nodes'].append({'id': 9, 'type': 'ellipsis'})
        prepared_input = record(data)
        with self.assertRaisesRegex(ValueError, 'unreachable'):
            index.decode(prepared_input)
        for name, limit in (('MAX_NODES', 7), ('MAX_EDGES', 2), ('MAX_DEPTH', 1)):
            with self.subTest(bound=name), mock.patch.object(index, name, limit):
                prepared_input = record()
                with self.assertRaises(ValueError):
                    index.decode(prepared_input)

    def test_cached_shared_subgraphs_cannot_hide_a_longer_path_over_the_depth_limit(self) -> None:
        for wrappers in (6, 7):
            data = graph()
            # Visit the long shared suffix first, so a later longer path will
            # reuse its cached height rather than traversing the suffix again.
            for identity in range(9, 129):
                data['nodes'].append({'id': identity, 'type': 'tuple',
                                      'items': [identity + 1 if identity < 128 else 3]})
            previous = 9
            for identity in range(129, 129 + wrappers):
                data['nodes'].append({'id': identity, 'type': 'tuple', 'items': [previous]})
                previous = identity
            data['nodes'][1]['items'] = [9, previous]
            with self.subTest(wrappers=wrappers):
                if wrappers == 6:
                    self.assertEqual(index.decode(record(data)).root, 1)
                else:
                    prepared_input = record(data)
                    with self.assertRaisesRegex(ValueError, 'graph depth'):
                        index.decode(prepared_input)

    def test_recorded_filesystem_codec_does_not_assume_the_verifier_codec(self) -> None:
        data = graph()
        data['filesystem'] = {'encoding': 'iso8859-1', 'errors': 'strict'}
        data['nodes'][4]['value'] = '/source/caf\u00e9.py'
        value = record(data, path=b'/source/caf\xe9.py')
        self.assertEqual(index.decode(value).filesystem_encoding, 'iso8859-1')
        prepared_input = record(data, path=b'/source/caf\xc3\xa9.py')
        with self.assertRaisesRegex(ValueError, 'filename bytes differ'):
            index.decode(prepared_input)

    def test_unknown_or_malformed_filesystem_codec_fields_refuse(self) -> None:
        for filesystem in ({}, None, {'encoding': 'utf-8'},
                           {'encoding': 'utf-8', 'errors': 'strict', 'extra': 1},
                           {'encoding': '', 'errors': 'strict'},
                           {'encoding': 'utf-8', 'errors': None},
                           {'encoding': 'x' * 129, 'errors': 'strict'},
                           {'encoding': 'bad/codec', 'errors': 'strict'},
                           {'encoding': 'utf-8', 'errors': 'nonexistent_handler'},
                           {'encoding': 'nonexistent_codec', 'errors': 'strict'}):
            data = graph();data['filesystem'] = filesystem
            with self.subTest(filesystem=filesystem):
                prepared_input = record(data)
                with self.assertRaises(ValueError):
                    index.decode(prepared_input)

    def test_filename_mismatch_nul_and_bad_encoding_refuse_surrogateescape_passes(self) -> None:
        for name in ('different.py', '/source/app.py\0suffix', '\ud800'):
            data = graph();data['nodes'][4]['value'] = name
            with self.subTest(name=name):
                prepared_input = record(data)
                with self.assertRaises(ValueError):
                    index.decode(prepared_input)
        data = graph();data['nodes'][4]['value'] = '/source/app.py\udcff'
        value = record(data, path=b'/source/app.py\xff')
        self.assertEqual(index.decode(value).nodes[4].value, os.fsdecode(value.path))


if __name__ == '__main__':
    unittest.main()
