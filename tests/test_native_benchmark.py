"""Actual stock/native workloads, independent checksum, controls and drain refusals."""
from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from otelc import benchmark, benchmark_process, control, native_benchmark as adapter, native_benchmark_launch as launch
from otelc import native_gate, native_launch
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


def policy(*, lifetimes: bool = True) -> Policy:
    """Identical configuration for every example except explicit lifetime comparisons."""
    data = copy.deepcopy(Policy.load(ROOT / 'examples/otelc.toml').data)
    data['lifetimes']['enabled'] = lifetimes
    data['benchmark'].update(iterations=10, samples=4, warmup=1)
    data['metrics'].update(collection=False, emission=False)
    data['traces'].update(enabled=True, sample_ratio=1)
    data['export'].update(interval_ms=60000)
    return Policy.parse(data)


def drain() -> dict:
    """Closed SDK shape; mutations below must not turn unknown/loss into success."""
    exports = {'exporters_observed': 1, 'batches_started': '1', 'batches_succeeded': '1',
        'batches_failed': '0', 'batches_pending': '0', 'units_started': '2',
        'units_succeeded': '2', 'units_failed': '0'}
    return {'schema_version': 1, 'kind': 'otelc.runtime-health', 'language': 'cpp', 'scope': 'sdk_pipeline',
        'pid': 101, 'phase': 'final', 'sdk_attached': True, 'source_complete': False,
        'policy_sha256': '0'*64, 'selection_sha256': '1'*64,
        'lifecycle': {'flush': 'succeeded', 'shutdown': 'succeeded'},
        'metric_flush': {'attempts': '1', 'failed_attempts': '0'},
        'span_accounting': {'processors_observed': 1, 'batch_processors_observed': 1,
            'queue_evidence': 'derived_after_shutdown', 'sampled_started': '2', 'sampled_ended': '2',
            'sampled_active': '0', 'queue_pending': '0', 'queue_dropped': '0'},
        'trace_exports': {**exports, 'unit': 'spans'}, 'metric_exports': {**exports, 'unit': 'metric_points'},
        'function_accounting': {'registered': '1', 'started': '1', 'ended': '1', 'active': '0',
            'lost': '0', 'invalid': '0', 'metadata_errors': '0', 'closed': True},
        'object_accounting': {'types': '1', 'admitted': '1', 'completed': '1', 'live': '0',
            'destroying': '0', 'ending': '0', 'censored': '0', 'lost': '0', 'duplicates': '0',
            'invalid': '0', 'closing': True, 'closed': True},
        'downstream_receipt': 'unavailable', 'otlp_partial_rejection': 'unavailable'}


class NativeBenchmarkAccountingTests(unittest.TestCase):
    def test_export_barriers_require_real_counter_or_native_effective_state(self):
        self.assertTrue(benchmark.emission_stopped({'in_flight_exports': 0}))
        self.assertFalse(benchmark.emission_stopped({'in_flight_exports': 1}))
        self.assertTrue(benchmark.emission_stopped({'effective_emission_off': True}))
        self.assertFalse(benchmark.emission_stopped({'effective_emission_off': False}))
        for state in ({}, {'in_flight_exports': None}, {'in_flight_exports': False},
                      {'in_flight_exports': -1}, {'effective_emission_off': 1}):
            with self.subTest(state=state), self.assertRaises(ValueError): benchmark.emission_stopped(state)

    def test_incomplete_failed_unknown_and_censored_drain_cannot_be_published(self):
        valid = drain(); adapter.validate_drain(valid, policy())
        mutations = [('lifecycle', 'shutdown', 'timed_out'), ('metric_flush', 'failed_attempts', '1'),
            ('function_accounting', 'closed', False), ('function_accounting', 'ended', '0'),
            ('function_accounting', 'lost', '1'), ('span_accounting', 'queue_evidence', 'unavailable'),
            ('span_accounting', 'sampled_ended', '1'), ('span_accounting', 'queue_pending', None),
            ('trace_exports', 'units_succeeded', '1'), ('trace_exports', 'batches_succeeded', '0'),
            ('metric_exports', 'units_failed', '1'), ('metric_exports', 'units_succeeded', None),
            ('object_accounting', 'closed', False), ('object_accounting', 'completed', '0'),
            ('object_accounting', 'lost', '1'), ('object_accounting', 'censored', '1')]
        current = policy()
        for group, name, value in mutations:
            wrong = copy.deepcopy(valid); wrong[group][name] = value
            with self.subTest(group=group, name=name), self.assertRaises(ValueError):
                adapter.validate_drain(wrong, current)
        for wrong in (None, valid | {'phase': 'running'}, valid | {'sdk_attached': False}):
            with self.assertRaises(ValueError): adapter.validate_drain(wrong, current)
        wrong = copy.deepcopy(valid); wrong.pop('object_accounting')
        without_lifetimes = policy(lifetimes=False)
        with self.assertRaises(ValueError): adapter.validate_drain(wrong, current)
        with self.assertRaises(ValueError): adapter.validate_drain(valid, without_lifetimes)

    def test_embedded_sdk_has_no_synthetic_pid_and_external_sdk_identity_stays_strict(self):
        self.assertTrue(benchmark_process.sdk_identity({}, None))
        self.assertFalse(benchmark_process.sdk_identity({'sdk_pid': 202}, None))
        self.assertTrue(benchmark_process.sdk_identity({'sdk_pid': 202}, 202))
        for value in (None, True, 202., '202', 203):
            self.assertFalse(benchmark_process.sdk_identity({'sdk_pid': value}, 202))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(benchmark_process.control, 'request', return_value={
                    'pid': 101, 'collection': True, 'emission': True}):
                benchmark_process.finalize_metrics(Mock(pid=101), output / 'control.json', None, output)
            self.assertEqual(set(json.loads((output / 'metric-finalization.json').read_text())),
                             {'schema_version', 'restored', 'flushed'})

    def test_unsupported_native_context_refuses_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'run'
            data = copy.deepcopy(policy().data); data['context']['tasks'] = True
            current, sdk = Policy.parse(data), Path('/no-sdk')
            with self.assertRaisesRegex(ValueError, 'propagation'):
                adapter.run(current, output, language='c', sdk_directory=sdk)
            self.assertFalse(output.exists())

    def test_c_resource_drain_requires_closed_free_boundaries_and_no_censoring(self):
        valid = drain()
        valid['language'] = 'c'
        valid['resource_accounting'] = valid.pop('object_accounting')
        valid['resource_accounting']['releasing'] = valid['resource_accounting'].pop('destroying')
        adapter.validate_drain(valid, policy())
        for key in ('releasing', 'censored', 'lost', 'invalid'):
            changed = copy.deepcopy(valid); changed['resource_accounting'][key] = '1'
            with self.subTest(key=key), self.assertRaises(ValueError):
                adapter.validate_drain(changed, policy())


class NativeBenchmarkIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.disk_usage(ROOT).free < 8 * 1024**3:
            raise ValueError('Native benchmark checks require at least 8 GiB free')
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.capture_diagnostics = os.environ.get('OTELC_NATIVE_BENCHMARK_CAPTURE') == '1'
        cls.keep = os.environ.get('OTELC_CPP_ANALYSIS') == '1' or cls.capture_diagnostics
        cls.temporary = tempfile.TemporaryDirectory(prefix='native-benchmark-tests-',
            dir=ROOT / 'build', delete=not cls.keep)
        cls.base = Path(cls.temporary.name).resolve()
        cls.sdk = Path(os.environ.get('OTELC_BENCHMARK_SDK', str(cls.base / 'sdk')))
        if not os.environ.get('OTELC_BENCHMARK_SDK'):
            native_launch.build_sdk(cls.sdk)

    @classmethod
    def tearDownClass(cls):
        if cls.keep: cls.temporary._finalizer.detach()
        else: cls.temporary.cleanup()

    def setUp(self):
        directory = self.base / (self._testMethodName + '-otlp') if self.capture_diagnostics else None
        self.capture = Capture(record_directory=directory); self.addCleanup(self.capture.close)
        self.data = copy.deepcopy(policy().data)
        self.data['export']['endpoint'] = f'http://127.0.0.1:{self.capture.server.server_port}'

    def run_benchmark(self, language, *, lifetimes=True, publish=True):
        self.data['lifetimes']['enabled'] = lifetimes
        output = self.base / (self._testMethodName + '-' + language)
        original_request = control.request

        def request(endpoint: Path, command: str) -> dict:
            try:
                return original_request(endpoint, command)
            except Exception as failure:
                if self.capture_diagnostics and command == 'flush':
                    try:
                        diagnostic = {'command': command, 'recorded_at_unix_ns': time.time_ns()}
                        try:
                            diagnostic['health'] = original_request(endpoint, 'health')
                        except Exception as error:
                            diagnostic['health_error_type'] = type(error).__name__
                        path = output / 'failed-flush.json'
                        path.write_text(json.dumps(diagnostic, indent=2) + '\n')
                        path.chmod(0o600)
                    except Exception as error:
                        failure.add_note('Native benchmark diagnostic capture failed: ' + type(error).__name__)
                raise

        with patch.object(control, 'request', side_effect=request):
            report = adapter.run(Policy.parse(self.data), output, language=language,
                sdk_directory=self.sdk, comparison_id='shared-native-smoke', publish=publish)
        return output, report

    def test_actual_c_and_cpp_match_the_shared_workload_and_all_four_metric_states(self):
        for language in ('c', 'cpp'):
            with self.subTest(language=language):
                output, report = self.run_benchmark(language, lifetimes=False)
                self.assertEqual(report['expected_checksum'], 41284864)
                self.assertEqual(set(report['samples_ns_per_operation']), {'plain', *benchmark.STATES})
                self.assertTrue(all(len(samples) == 4 for samples in report['samples_ns_per_operation'].values()))
                self.assertEqual({row['pid'] for row in report['transitions']}, {report['pids']['instrumented']})
                self.assertIsNone(report['sdk_pid'])
                self.assertIsNone(report['supervisor_pids']['plain'])
                self.assertNotEqual(report['supervisor_pids']['instrumented'], report['pids']['instrumented'])
                self.assertEqual(len(report['process_samples']), 20)
                for row in report['process_samples']:
                    roles = {'application'} if row['mode'] == 'plain' else {'application', 'supervisor'}
                    self.assertEqual(set(row['process_usage']), roles)
                    self.assertEqual(row['checksum'], report['expected_checksum'])
                    self.assertTrue(all(usage['rss_bytes'] > 0 for usage in row['process_usage'].values()))
                self.assertFalse((output / 'plain/app').exists())
                self.assertFalse((output / 'plain/native-request.json').exists())
                self.assertIn('native-execution.json', report['launch_evidence']['instrumented'])
                self.assertEqual(report['launch_to_first_response']['plain']['calls'], 1)
                self.assertEqual(report['sdk_health']['function_accounting']['ended'], '171')
                points = self.capture.points('otelc.function.calls')
                self.assertTrue(points)
                self.assertEqual(max(point.as_int for point in points), 80)
                self.assertTrue(self.capture.points('otelc.benchmark.operation.duration'))

    def test_cpp_lifetime_cost_uses_the_common_policy_and_true_destruction(self):
        output, report = self.run_benchmark('cpp')
        objects = report['sdk_health']['object_accounting']
        self.assertEqual((objects['admitted'], objects['completed'], objects['censored']), ('171', '171', '0'))
        self.assertTrue(self.capture.points('otelc.lifetime.completed'))
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        lifetimes = [span for span in spans if span.name == 'WorkItem lifetime']
        self.assertEqual(len(lifetimes), 171)
        self.assertTrue(all(len(span.links) == 1 for span in lifetimes))
        source = ROOT / 'examples/cpp/BenchmarkApp.cpp'
        self.assertEqual(report['original_source'], native_gate.identity(source))
        self.assertEqual(report['source_sha256'], native_gate.identity(output / 'source/examples/cpp/BenchmarkApp.cpp')['sha256'])

    def test_c_allocation_cost_uses_the_common_policy_and_real_free_boundaries(self):
        output, report = self.run_benchmark('c')
        objects = report['sdk_health']['resource_accounting']
        self.assertEqual((objects['admitted'], objects['completed'], objects['censored']), ('171', '171', '0'))
        self.assertTrue(self.capture.points('otelc.lifetime.completed'))
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        lifetimes = [span for span in spans if span.name == 'WorkItem lifetime']
        self.assertEqual(len(lifetimes), 171)
        for span in lifetimes:
            attrs = {item.key: item.value.string_value for item in span.attributes}
            self.assertEqual(attrs['boundary'], 'free_entry')
            self.assertEqual(len(span.links), 1)
        self.assertEqual(report['original_source'], native_gate.identity(ROOT / 'examples/c/BenchmarkApp.c'))
        self.assertEqual(report['source_sha256'], native_gate.identity(output / 'source/examples/c/BenchmarkApp.c')['sha256'])

    def test_failed_checksum_still_drains_and_refuses_report_or_publication(self):
        original = benchmark.batch
        calls = 0
        def wrong(process, count):
            nonlocal calls
            calls += 1
            result = original(process, count)
            return {**result, 'checksum': -1} if calls == 9 else result
        with patch.object(benchmark, 'batch', side_effect=wrong), patch.object(benchmark, 'export_result') as publish:
            with self.assertRaisesRegex(ValueError, 'checksum differs'):
                self.run_benchmark('cpp', lifetimes=False)
            publish.assert_not_called()
        output = self.base / (self._testMethodName + '-cpp')
        self.assertFalse((output / 'report.json').exists())
        self.assertTrue((output / 'instrumented/native-execution.json').exists())
        health = json.loads((output / 'instrumented/app/health.json').read_text())
        self.assertEqual(health['lifecycle']['shutdown'], 'succeeded')

    def test_final_observation_loss_prevents_publication_after_owned_shutdown(self):
        original = adapter.validate_drain
        def lost(report, policy):
            changed = copy.deepcopy(report)
            changed['function_accounting']['lost'] = '1'
            original(changed, policy)
        with patch.object(adapter, 'validate_drain', side_effect=lost), patch.object(benchmark, 'export_result') as publish:
            with self.assertRaisesRegex(ValueError, 'observation loss'):
                self.run_benchmark('cpp', lifetimes=False)
            publish.assert_not_called()
        output = self.base / (self._testMethodName + '-cpp')
        self.assertTrue((output / 'instrumented/native-execution.json').exists())
        self.assertFalse((output / 'report.json').exists())
        self.assertEqual(len((output / 'raw.jsonl').read_text().splitlines()), 20)
        self.assertTrue((output / 'metric-finalization.json').exists())

    def test_real_trace_export_rejection_retains_failed_sdk_health_and_no_report(self):
        self.capture.trace_response_code = 503
        self.data['benchmark'].update(iterations=2, samples=1, warmup=1)
        self.data['export'].update(timeout_ms=100, shutdown_timeout_ms=3000)
        with patch.object(benchmark, 'export_result') as publish:
            with self.assertRaises((ValueError, RuntimeError)):
                self.run_benchmark('cpp', lifetimes=False)
            publish.assert_not_called()
        self.assertTrue(self.capture.trace_request_seen.is_set())
        output = self.base / (self._testMethodName + '-cpp')
        final = json.loads((output / 'instrumented/app/health.json').read_text())
        self.assertGreater(int(final['trace_exports']['batches_failed']), 0)
        self.assertFalse((output / 'report.json').exists())

    def test_public_cli_selects_c_with_the_same_policy_and_matching_sdk(self):
        self.data['benchmark'].update(iterations=2, samples=1, warmup=1)
        self.data['lifetimes']['enabled'] = False
        # The common policy example is TOML; only endpoint/sample values differ in this test.
        config = self.base / 'cli.toml'
        config.write_text('schema_version=3\nlanguages=["c","cpp"]\n[sources]\ninclude=["examples/**"]\n'
            '[functions]\ninclude=["*workload*"]\n[traces]\nenabled=true\nsample_ratio=1\n'
            '[lifetimes]\nenabled=false\n[benchmark]\niterations=2\nsamples=1\nwarmup=1\n'
            f'[export]\nendpoint="{self.data["export"]["endpoint"]}"\ninterval_ms=60000\n')
        output = self.base / 'cli-output'
        command = [str(ROOT / 'bin/quux-otelc'), '--config', str(config), 'benchmark', '--language', 'c',
                   '--sdk-build', str(self.sdk), '--output', str(output), '--no-publish', '--comparison-id', 'native-cli']
        result = subprocess.run(command, env={**os.environ, 'OTELC_PYTHON': os.sys.executable},
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((output / 'report.json').read_text())
        self.assertEqual(report['comparison_id'], 'native-cli')
        self.assertEqual(report['language'], 'c')
        self.assertEqual(len(report['process_samples']), 5)
