"""The SDK handoff accepts a closed, bounded primitive telemetry profile."""

from __future__ import annotations

import copy
import unittest

from otelc import python_sdk_protocol as wire


def context(**changes):
    return {'trace_id': '1'.zfill(32), 'span_id': '2'.zfill(16),
            'trace_flags': 1, 'remote': False, **changes}


def metric(**changes):
    return {'kind': 'metric', 'scope': 'fixture', 'instrument': 'counter',
            'name': 'otelc.function.calls', 'value': 1, **changes}


def span(**changes):
    return {'name': 'fixture.workload', 'context': context(),
            'start_time': 1000, 'end_time': 2000, **changes}


class PythonSdkProtocolTests(unittest.TestCase):
    def test_valid_metric_kinds_keep_primitives_and_context(self) -> None:
        attributes = {'unicode': 'α😀', 'count': 2, 'active': True, 'ratio': .5,
                      'empty': [], 'names': ['α', 'β'], 'values': [1, 2],
                      'flags': [True, False], 'fractions': [.1, .2]}
        for kind in wire.COUNTERS:
            value = metric(instrument=kind, attributes=attributes,
                           context=context(remote=True, trace_flags=0),
                           version='1.2', schema_url='https://example.test/schema',
                           unit='s', description='Original metadata',
                           scope_attributes={'role': 'library'})
            self.assertIs(wire.metric(value), value)
            self.assertEqual(value['attributes'], attributes)
        self.assertEqual(wire.metric(metric(instrument='up_down_counter', value=-2))['value'], -2)
        self.assertIsNone(wire.context(None))

    def test_invalid_metric_shapes_or_values_fail_before_sdk_registration(self) -> None:
        changes = ({'unknown': 1}, {'scope': ''}, {'scope': 1},
                   {'instrument': 'observable_counter'}, {'name': '1invalid'},
                   {'value': True}, {'value': float('inf')}, {'value': -1},
                   {'value': 2 ** 63}, {'unit': 'µs'}, {'version': 'x' * 129},
                   {'description': 'x' * 1025}, {'scope_attributes': []})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                wire.metric(metric(**change))
        missing = metric()
        del missing['value']
        with self.assertRaises(ValueError):
            wire.metric(missing)

    def test_attribute_limits_and_unsupported_values_are_explicit(self) -> None:
        invalid = ({'bad': None}, {'bad': object()}, {'bad': {}},
                   {'bad': [1, True]}, {'bad': [[1]]}, {'bad': [1] * 129},
                   {'bad': float('nan')}, {1: 'not a string key'},
                   {'x' * 1025: 1}, {'text': 'x' * 4097},
                   {str(index): 1 for index in range(129)})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                wire.attributes(value)
        for value in (None, [], True):
            with self.assertRaises(ValueError):
                wire.attributes(value)

    def test_completed_span_keeps_parent_links_events_status_and_drop_counts(self) -> None:
        value = span(parent=context(span_id='3'.zfill(16), remote=True),
                     attributes={'code.function.name': 'α'},
                     scope='fixture', version='1.0', schema_url='',
                     scope_attributes={'library': 'kept'},
                     events=[{'name': 'exception', 'timestamp': 1500,
                              'attributes': {'exception.type': 'ValueError'}}],
                     links=[{'context': context(trace_id='4'.zfill(32)),
                             'attributes': {'relation': 'resource'}}],
                     status='ERROR', description='Literal description',
                     span_kind='SERVER', dropped_attributes=2,
                     dropped_events=3, dropped_links=4)
        before = copy.deepcopy(value)
        self.assertIs(wire.span(value), value)
        self.assertEqual(value, before)

    def test_invalid_span_identity_boundaries_and_metadata_are_refused(self) -> None:
        changes = ({'unknown': True}, {'name': ''}, {'context': None},
                   {'context': context(remote=True)}, {'start_time': True},
                   {'end_time': 999}, {'end_time': 2 ** 63},
                   {'parent': context()},
                   {'parent': context(trace_id='4'.zfill(32), span_id='3'.zfill(16))},
                   {'status': 'MAYBE'}, {'span_kind': 'BAD'},
                   {'scope': ''}, {'dropped_attributes': 2 ** 32},
                   {'events': {}}, {'events': [{}]}, {'events': [
                       {'name': 'event', 'timestamp': 0}]},
                   {'events': [{'name': 'event', 'timestamp': 1}] * 129},
                   {'links': {}}, {'links': [{'context': None}]},
                   {'links': [{'context': context()}] * 129})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                wire.span(span(**change))

    def test_context_id_flags_and_primitive_helpers_are_strict(self) -> None:
        for changes in ({'unknown': 1}, {'trace_id': '0' * 32},
                        {'span_id': 'A' * 16}, {'trace_flags': 256},
                        {'trace_flags': True}, {'remote': 1},
                        {'trace_state': 'x' * 513}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                wire.context(context(**changes))
        for value in (False, -1, 2 ** 63):
            with self.assertRaises(ValueError):
                wire.integer(value)
        for value in (0, None, 'true'):
            with self.assertRaises(ValueError):
                wire.flag(value)
        with self.assertRaises(ValueError):
            wire.fields([], set())
        self.assertEqual(wire.number(-2 ** 63), -2 ** 63)
        self.assertEqual(wire.number(.25), .25)
