"""Independent graph fixtures exercise values, aliases and unordered choices."""

from __future__ import annotations

import copy
import dataclasses
import unittest
from unittest import mock

from otelc.python_code_comparison import compare
from otelc.python_code_index import decode
from test_python_code_index import graph, record


def unordered() -> dict:
    """Two equal-bit NaNs, one also referenced outside their frozenset."""
    data = graph()
    data['nodes'][1]['items'] = [9, 10]
    data['nodes'].extend([
        {'id': 9, 'type': 'frozenset', 'items': [10, 11]},
        {'id': 10, 'type': 'float', 'data': '7ff8000000000001'},
        {'id': 11, 'type': 'float', 'data': '7ff8000000000001'},
    ])
    # None is still reachable through an additional constant.
    data['nodes'][1]['items'].append(3)
    return data


class PythonCodeComparisonTests(unittest.TestCase):
    def test_local_ids_offsets_and_filesystem_envelopes_do_not_define_values(self) -> None:
        first = decode(record())
        data = graph()
        # Swap name and filename IDs without changing their typed values.
        data['nodes'][4], data['nodes'][5] = data['nodes'][5], data['nodes'][4]
        for identity, node in enumerate(data['nodes'], 1):
            node['id'] = identity
        data['nodes'][0]['fields'].update(co_filename=6, co_name=5, co_qualname=5)
        data['filesystem'] = {'encoding': 'ascii', 'errors': 'strict'}
        other = decode(record(data, identity=99))
        self.assertEqual(compare(first, other).status, 'match')

    def test_metadata_filename_name_bytecode_and_constants_must_match(self) -> None:
        cases = []
        for field in ('co_flags', 'co_firstlineno', 'co_stacksize'):
            data = graph(); data['nodes'][0]['metadata'][field] += 1; cases.append(data)
        data = graph(); data['nodes'][5]['value'] = 'other'; cases.append(data)
        data = graph(); data['nodes'][7]['data'] = '970065005300'; cases.append(data)
        data = graph(); data['nodes'][2]['value'] = False; cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(compare(decode(record()), decode(record(data))).status, 'different')
        renamed = graph(); renamed['nodes'][4]['value'] = '/source/other.py'
        self.assertEqual(compare(decode(record()), decode(record(
            renamed, path=b'/source/other.py'))).status, 'different')

    def test_shared_and_separate_equal_nodes_are_different_in_both_directions(self) -> None:
        data = graph()
        data['nodes'].append({'id': 9, 'type': 'unicode', 'value': 'workload'})
        data['nodes'][0]['fields']['co_qualname'] = 9
        a, b = decode(record()), decode(record(data))
        self.assertEqual(compare(a, b).status, 'different')
        self.assertEqual(compare(b, a).status, 'different')

    def test_unordered_matching_backtracks_for_later_outer_alias(self) -> None:
        a = unordered(); b = copy.deepcopy(a)
        b['nodes'][8]['items'].reverse()
        self.assertEqual(compare(decode(record(a)), decode(record(b))).status, 'match')
        # Break the outer alias while keeping both frozenset element values.
        b['nodes'].append({'id': 12, 'type': 'float', 'data': '7ff8000000000001'})
        b['nodes'][1]['items'][1] = 12
        self.assertEqual(compare(decode(record(a)), decode(record(b))).status, 'different')

    def test_large_unambiguous_frozenset_does_not_consume_branch_depth(self) -> None:
        data = graph()
        data['nodes'][1]['items'].append(9)
        data['nodes'].append({'id': 9, 'type': 'frozenset', 'items': list(range(10, 310))})
        for identity in range(10, 310):
            number = identity.to_bytes(identity.bit_length() // 8 + 1, 'big', signed=True)
            data['nodes'].append({'id': identity, 'type': 'integer', 'data': number.hex()})
        other = copy.deepcopy(data); other['nodes'][8]['items'].reverse()
        self.assertEqual(compare(decode(record(data)), decode(record(other))).status, 'match')

    def test_exact_float_bits_and_container_order_are_observable(self) -> None:
        data = unordered(); other = copy.deepcopy(data)
        other['nodes'][9]['data'] = '7ff8000000000002'
        self.assertEqual(compare(decode(record(data)), decode(record(other))).status, 'different')
        other = copy.deepcopy(data); other['nodes'][1]['items'].reverse()
        self.assertEqual(compare(decode(record(data)), decode(record(other))).status, 'different')

    def test_version_and_budget_failures_are_incomplete_not_matches(self) -> None:
        value = decode(record())
        for changes, reason in (({'python_version': value.python_version + 1}, 'python-version'),
                                ({'descriptor_version': 1}, 'descriptor-version')):
            result = compare(value, dataclasses.replace(value, **changes))
            self.assertEqual((result.status, result.reason), ('incomplete', reason))
        self.assertEqual(compare(value, value, budget=1).status, 'incomplete')
        for budget in (True, 0, -1, 1_000_001, '1'):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                compare(value, value, budget=budget)
        with self.assertRaises(TypeError):
            compare(object(), value)

    def test_comparison_never_compiles_or_constructs_code(self) -> None:
        a, b = decode(record()), decode(record())
        with mock.patch('builtins.compile', side_effect=AssertionError('compile')), \
             mock.patch('builtins.__import__', side_effect=AssertionError('import')):
            self.assertEqual(compare(a, b).status, 'match')

    def test_hash_collisions_cannot_replace_exact_value_and_alias_comparison(self) -> None:
        a = graph(); b = graph(); b['nodes'][2]['value'] = False
        class Digest:
            def digest(self):
                return b'collision'
        left, changed = decode(record(a)), decode(record(b))
        b = graph()
        b['nodes'].append({'id': 9, 'type': 'unicode', 'value': 'workload'})
        b['nodes'][0]['fields']['co_qualname'] = 9
        separate = decode(record(b))
        with mock.patch('otelc.python_code_comparison.hashlib.sha256', return_value=Digest()):
            self.assertEqual(compare(left, changed).status, 'different')
            self.assertEqual(compare(left, separate).status, 'different')


if __name__ == '__main__':
    unittest.main()
