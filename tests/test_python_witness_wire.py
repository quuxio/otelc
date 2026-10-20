"""Strict witness record boundaries do not imply a runtime scoped capability."""
import base64
import copy
import json
import unittest

from test_python_adapter import ROOT
from quux_otelc_python.witness_wire import (
    MAX_INTEGER, ProtocolError, decode_record, decode_request, encode_record,
)


class PythonWitnessWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads((ROOT / 'tests/fixtures/witness-control.json').read_text())['cases']

    def test_common_framing_and_structural_vectors(self):
        self.assertEqual(len({case['name'] for case in self.cases}), len(self.cases))
        self.assertEqual(sum(case['valid'] for case in self.cases), 16)
        self.assertEqual(len(self.cases), 43)
        for case in self.cases:
            with self.subTest(case=case['name']):
                frame = base64.b64decode(case['frame_base64'], validate=True)
                if not case['valid']:
                    with self.assertRaises(ProtocolError) as failure:
                        decode_record(frame)
                    self.assertLess(len(str(failure.exception)), 40)
                    continue
                value = decode_record(frame)
                self.assertEqual(decode_record(encode_record(value)), value)
                if value['kind'] == 'request':
                    self.assertEqual(decode_request(frame), value)
                else:
                    with self.assertRaises(ProtocolError):
                        decode_request(frame)

    def test_invalid_otlp_ids_and_response_discriminators_are_refused(self):
        record = decode_record(base64.b64decode(self.cases[9]['frame_base64']))
        for field, value in (('root_trace_id', '0' * 32), ('root_span_id', '0' * 16),
                             ('event', []), ('kind', {}), ('event_seq', MAX_INTEGER + 1)):
            with self.subTest(field=field):
                malformed = dict(record, **{field: value})
                with self.assertRaises(ProtocolError):
                    encode_record(malformed)
        for index, field in ((3, 'available'), (5, 'ok')):
            record = decode_record(base64.b64decode(self.cases[index]['frame_base64']))
            record[field] = 1
            with self.assertRaises(ProtocolError):
                encode_record(record)

    def test_nested_duplicates_nonfinite_tokens_and_recursive_json_are_redacted(self):
        end = base64.b64decode(self.cases[12]['frame_base64'])
        nested_duplicate = end.replace(b'"inflight":0', b'"inflight":0,"inflight":0')
        with self.assertRaises(ProtocolError) as duplicate:
            decode_record(nested_duplicate)
        self.assertEqual(duplicate.exception.code, 'duplicate_field')
        arm = base64.b64decode(self.cases[1]['frame_base64'])
        for number in (b'NaN', b'Infinity', b'-Infinity', b'1.0', b'1e0'):
            with self.subTest(number=number), self.assertRaises(ProtocolError):
                decode_record(arm.replace(b'"ready_seq":1', b'"ready_seq":' + number))
        with self.assertRaises(ProtocolError) as recursive:
            decode_record(b'[' * 3000 + b']' * 3000 + b'\n')
        self.assertEqual(str(recursive.exception), 'malformed_record')
        self.assertEqual(str(ProtocolError('arbitrary private contents')), 'malformed_record')

    def test_health_and_metadata_never_accept_arbitrary_extra_payloads(self):
        end = decode_record(base64.b64decode(self.cases[12]['frame_base64']))
        for health in ([], {}, dict(end['health'], inflight=True),
                       dict(end['health'], losses=-1), dict(end['health'], export_finished=1),
                       dict(end['health'], exception_message='private')):
            with self.subTest(health=health), self.assertRaises(ProtocolError):
                encode_record(dict(end, health=health))
        for value in (None, [], 1, {'kind': 'response', 'op': 'unknown', 'ok': False},
                      dict(end, outcome='arbitrary private text'), dict(end, request_id='1' * 32)):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                encode_record(value)

    def test_valid_records_do_not_establish_freshness_or_completion_order(self):
        # Replay/phase/context enforcement belongs to the producer state machine,
        # which is not implemented or advertised by this codec foundation.
        arm = base64.b64decode(self.cases[1]['frame_base64'])
        first, second = decode_request(arm), decode_request(arm)
        self.assertEqual(first, second)
        changed = copy.deepcopy(first)
        changed['wrapper_probe_id'] = '0' * 64
        self.assertEqual(decode_request(encode_record(changed)), changed)
        self.assertEqual(decode_record(base64.b64decode(self.cases[14]['frame_base64']))['complete'], True)
