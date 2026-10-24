"""Go health is versioned raw SDK evidence, never a delivery guarantee."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from otelc import go_health, health


def report() -> dict:
    exports = {'observed': True, 'attempts': '1', 'succeeded': '1', 'failed': '0', 'closed': False}
    return {'schema_version': 3, 'kind': 'otelc.runtime-health', 'language': 'go',
            'scope': 'sdk_pipeline', 'pid': os.getpid(), 'policy_sha256': 'a' * 64,
            'selection_sha256': 'b' * 64, 'phase': 'running', 'sdk_attached': True,
            'source_complete': False,
            'controls': {'schema_version': 1, 'pid': os.getpid(), 'collection': False,
                         'emission': True, 'effective_emission_off': False},
            'lifecycle': {'flush': 'not_started', 'shutdown': 'not_started'},
            'metric_flush': {'attempts': '0', 'failed_attempts': '0'},
            'probe_accounting': dict.fromkeys(go_health.PROBE_COUNTERS, '0') | {'closed': False},
            'metric_exports': exports,
            'trace_exports': exports | {'exported_spans': '1', 'queue_pending': '0', 'queue_dropped': '0'},
            'downstream_receipt': 'unavailable', 'otlp_partial_rejection': 'unavailable'}


class GoHealthTests(unittest.TestCase):
    def test_common_health_routes_live_and_final_go_reports(self) -> None:
        for phase in ('running', 'final'):
            value = report() | {'phase': phase}
            self.assertIs(health.validate(value), value)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'health.json'
                path.write_text(json.dumps(value))
                path.chmod(0o600)
                self.assertEqual(health.read(path), value)
        value = report() | {'trace_exports': {'observed': False}}
        self.assertIs(go_health.validate(value), value)

    def test_refuses_wrong_identity_control_and_completion_claims(self) -> None:
        for field, invalid in (
            ('schema_version', True), ('schema_version', 2), ('language', 'java'),
            ('scope', 'all_application_code'), ('pid', True), ('pid', 0),
            ('policy_sha256', 'A' * 64), ('selection_sha256', []),
            ('phase', 'completed'), ('sdk_attached', False), ('source_complete', True),
            ('downstream_receipt', 'succeeded'), ('otlp_partial_rejection', 'zero'),
            ('lifecycle', {}), ('lifecycle', {'flush': [], 'shutdown': 'pending'}),
            ('metric_exports', {'observed': False}), ('trace_exports', None),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                go_health.validate(report() | {field: invalid})
        for value in (None, [], report() | {'unknown': True}):
            with self.assertRaises(ValueError):
                go_health.validate(value)
        for field, invalid in (('schema_version', True), ('pid', 1), ('collection', 0),
                               ('effective_emission_off', True), ('emission', None)):
            value = report()
            value['controls'][field] = invalid
            with self.subTest(field=field), self.assertRaises(ValueError):
                go_health.validate(value)

    def test_refuses_unknown_or_inexact_counters(self) -> None:
        for section, field in (('probe_accounting', 'observer_lost'),
                               ('metric_flush', 'attempts'), ('trace_exports', 'queue_pending')):
            for invalid in (-1, 0, True, None, '00', '-1', '1.0', '1e3', '١', str(1 << 64)):
                value = report()
                value[section][field] = invalid
                with self.subTest(section=section, invalid=invalid), self.assertRaises(ValueError):
                    go_health.validate(value)
        for section in ('probe_accounting', 'metric_flush', 'trace_exports'):
            value = copy.deepcopy(report())
            value[section]['unknown'] = '0'
            with self.assertRaises(ValueError):
                go_health.validate(value)
        value = report()
        value['probe_accounting']['closed'] = 0
        with self.assertRaises(ValueError):
            go_health.validate(value)
        value = report() | {'trace_exports': {'observed': False, 'attempts': '0'}}
        with self.assertRaises(ValueError):
            go_health.validate(value)

    def test_raw_snapshots_do_not_invent_live_atomic_oracle(self) -> None:
        value = report()
        # Independent live atomic loads are not a quiescent accounting proof.
        # Fixed consumers must separately wait for exit and inspect raw evidence.
        value['probe_accounting']['function_started'] = '1'
        value['probe_accounting']['function_ended'] = '2'
        value['probe_accounting']['function_active'] = str(go_health.MAX_UINT64)
        self.assertIs(go_health.validate(value), value)
