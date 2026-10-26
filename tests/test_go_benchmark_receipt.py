"""Refuse stale, partial or inconsistent retained benchmark observations."""
from __future__ import annotations

import copy
import statistics
import unittest

from ci import go_benchmark_receipt as receipt
from otelc import benchmark, benchmark_process
from otelc.config import Policy


class GoBenchmarkReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy.parse({'schema_version': 3,
            'benchmark': {'iterations': 10, 'samples': 4, 'warmup': 1}})
        self.raw, self.responses, transitions = [], [], []
        values = {mode: [] for mode in receipt.MODES}
        order = [('plain', sample, 'plain') for sample in range(4)]
        order += [('instrumented', sample, mode) for sample in range(4)
                  for mode in benchmark_process.state_order(sample, plain=False)]
        for index, (lane, sample, mode) in enumerate(order):
            pid, owner = (101, 102) if lane == 'plain' else (201, 202)
            timing = {'calls': 10, 'checksum': 41284864, 'elapsed_ns': 10000 + index}
            row = timing | {'sample': sample, 'mode': mode, 'pid': pid,
                            'process_usage': {role: self.usage(value)
                                for role, value in (('application', pid), ('supervisor', owner))}}
            self.raw.append(row)
            self.responses.append({'sample': sample, 'mode': mode, 'pid': pid, 'response': timing})
            values[mode].append(timing['elapsed_ns'] / 10)
            if lane == 'instrumented':
                collecting, emitting = benchmark.STATES[mode]
                transitions.append({'sample': sample, 'mode': mode, 'schema_version': 1,
                    'pid': pid, 'collection': collecting, 'emission': emitting,
                    'effective_emission_off': not emitting})
        self.value = {'schema_version': 2, 'language': 'go', 'workload': 'integer128-v1',
            'comparison_id': 'fresh-run', 'run_id': 'a' * 32, 'iterations': 10,
            'config': self.policy.data, 'source_complete': False, 'downstream_receipt': 'unavailable',
            'expected_checksum': 41284864, 'samples_ns_per_operation': values,
            'median_ns_per_operation': {mode: statistics.median(rows) for mode, rows in values.items()},
            'pids': {'plain': 101, 'instrumented': 201}, 'sdk_pid': 201,
            'supervisor_pids': {'plain': 102, 'instrumented': 202}, 'transitions': transitions,
            'process_samples': copy.deepcopy(self.raw)}

    @staticmethod
    def usage(pid: int) -> dict:
        before = {'pid': pid, 'cpu_ns': 5, 'rss_bytes': 4096, 'observed_at_unix_ns': 1000}
        after = before | {'cpu_ns': 5, 'rss_bytes': 8192, 'observed_at_unix_ns': 2000}
        return {'before': before, 'after': after, 'cpu_ns': 0, 'rss_bytes': 8192}

    def check(self, value: dict | None = None, raw: list | None = None,
              responses: list | None = None) -> dict:
        return receipt.report(self.value if value is None else value,
            self.raw if raw is None else raw, self.responses if responses is None else responses,
            self.policy, 'fresh-run')

    def vector(self, *, counts: bool, aggregated: bool = False) -> dict:
        rows = []
        for mode, samples in self.value['samples_ns_per_operation'].items():
            labels = {'language': 'go', 'mode': mode}
            if not aggregated:
                labels.update(comparison_id='fresh-run', run_id='a' * 32)
            value = len(samples) if counts else statistics.mean(samples)
            rows.append({'metric': labels, 'value': [1234.5, str(value)]})
        return {'status': 'success', 'data': {'resultType': 'vector', 'result': rows}}

    def test_complete_run_keeps_every_mode_pid_and_real_zero_cpu_window(self) -> None:
        result = self.check()
        self.assertEqual(result['samples_each'], 4)
        self.assertEqual(result['expected_checksum'], 41284864)
        self.assertEqual(result['modes'], list(receipt.MODES))
        for counts in (False, True):
            observed = receipt.vector(self.vector(counts=counts), self.value, counts=counts)
            self.assertEqual(set(observed), set(receipt.MODES))
        observed = receipt.vector(self.vector(counts=False, aggregated=True), self.value,
                                  counts=False, aggregated=True)
        self.assertEqual(set(observed), set(receipt.MODES))

    def test_foreign_policy_run_schema_workload_and_embedded_sdk_refuse(self) -> None:
        for key, replacement in (('schema_version', True), ('language', 'java'),
                ('comparison_id', 'old-run'), ('workload', 'different'), ('config', {}),
                ('source_complete', True), ('downstream_receipt', 'complete'),
                ('iterations', True), ('expected_checksum', True), ('sdk_pid', 202),
                ('samples_ns_per_operation', {}), ('pids', {'plain': 201, 'instrumented': 201}),
                ('supervisor_pids', {'plain': 101, 'instrumented': 202})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check(self.value | {key: replacement})

    def test_raw_counts_response_identity_and_derived_values_cannot_be_repaired(self) -> None:
        with self.assertRaises(ValueError): self.check(raw=self.raw[:-1])
        value = copy.deepcopy(self.value)
        value['process_samples'][0]['elapsed_ns'] += 1
        with self.assertRaises(ValueError): self.check(value)
        for key, replacement in (('pid', 999), ('sample', True), ('mode', 'both_on'),
                                 ('calls', 9), ('checksum', 0), ('elapsed_ns', 0)):
            raw = copy.deepcopy(self.raw)
            raw[0][key] = replacement
            value = self.value | {'process_samples': raw}
            with self.subTest(key=key), self.assertRaises(ValueError): self.check(value, raw)
        response = copy.deepcopy(self.responses)
        response[0]['response']['elapsed_ns'] += 1
        with self.assertRaises(ValueError): self.check(responses=response)
        for key in ('transitions', 'samples_ns_per_operation', 'median_ns_per_operation'):
            value = copy.deepcopy(self.value)
            value[key] = [] if key == 'transitions' else {}
            with self.subTest(key=key), self.assertRaises(ValueError): self.check(value)

    def test_missing_and_duplicate_sdk_resource_roles_and_changed_snapshots_refuse(self) -> None:
        for kind in ('extra-sdk', 'before-pid', 'after-cpu', 'rss', 'clock', 'bool-cpu'):
            raw = copy.deepcopy(self.raw)
            roles = raw[0]['process_usage']
            usage = roles['application']
            if kind == 'extra-sdk': roles['sdk'] = copy.deepcopy(usage)
            elif kind == 'before-pid': usage['before']['pid'] = 99
            elif kind == 'after-cpu': usage['after']['cpu_ns'] -= 1
            elif kind == 'rss': usage['rss_bytes'] = 4096
            elif kind == 'clock': usage['after']['observed_at_unix_ns'] = 999
            else: usage['cpu_ns'] = False
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.check(self.value | {'process_samples': raw}, raw)

    def test_partial_warning_stale_duplicate_and_nonfinite_vectors_refuse(self) -> None:
        for kind in ('status', 'warning', 'infos', 'type', 'missing', 'duplicate', 'run',
                     'comparison', 'language', 'count', 'nan', 'negative', 'timestamp', 'number'):
            value = self.vector(counts=True)
            rows = value['data']['result']
            if kind == 'status': value['status'] = 'error'
            elif kind == 'warning': value['warnings'] = ['partial']
            elif kind == 'infos': value['infos'] = ['omitted']
            elif kind == 'type': value['data']['resultType'] = 'matrix'
            elif kind == 'missing': rows.pop()
            elif kind == 'duplicate': rows[1] = copy.deepcopy(rows[0])
            elif kind == 'run': rows[0]['metric']['run_id'] = 'b' * 32
            elif kind == 'comparison': rows[0]['metric']['comparison_id'] = 'old-run'
            elif kind == 'language': rows[0]['metric']['language'] = 'python'
            elif kind == 'count': rows[0]['value'][1] = '3'
            elif kind == 'nan': rows[0]['value'][1] = 'NaN'
            elif kind == 'negative': rows[0]['value'][1] = '-1'
            elif kind == 'timestamp': rows[0]['value'][0] = True
            else: rows[0]['value'][1] = 4
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                receipt.vector(value, self.value, counts=True)

    def test_aggregated_panel_mean_requires_retained_values_and_never_admits_counts(self) -> None:
        vector = self.vector(counts=False, aggregated=True)
        vector['data']['result'][0]['value'][1] = '100000'
        with self.assertRaises(ValueError):
            receipt.vector(vector, self.value, counts=False, aggregated=True)
        with self.assertRaises(ValueError):
            receipt.vector(self.vector(counts=True, aggregated=True), self.value,
                           counts=True, aggregated=True)
        value = copy.deepcopy(self.value)
        value['samples_ns_per_operation']['plain'][0] = float('nan')
        with self.assertRaises(ValueError): self.check(value)


if __name__ == '__main__':
    unittest.main()
