"""Keep owned SDK randomness independent of a seeded application."""
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_python_adapter import Capture, ROOT
from test_python_spans import TraceCapture, receiver, trace_plan
from opentelemetry.context import Context, attach, detach
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from quux_otelc_python.randomness import PrivateIdGenerator, PrivateRandom, PrivateReservoir, isolate_exporter, metric_views
from opentelemetry.sdk.metrics._internal.exemplar.exemplar_reservoir import BucketIndexError
import requests
from quux_otelc_python.monitor import Monitor
from quux_otelc_python.telemetry import Runtime


SOURCE = '''import random
def target():
    return random.random()
def run(flush):
    random.seed(12345)
    values = []
    for _ in range(5):
        values.append(target())
        flush()
    return values, random.getstate()
'''


class PythonRandomStateTests(unittest.TestCase):
    def exercise(self, metrics, traces, flush=False, endpoint=None):
        namespace = {}
        exec(compile(SOURCE, str(ROOT / 'trace_fixture.py'), 'exec'), namespace)
        baseline = namespace['run'](lambda: None)
        policy = trace_plan()
        policy['metrics']['enabled'] = metrics
        policy['traces']['enabled'] = traces
        policy['function_matchers']['include'] = [r'(?-u)^trace_fixture\.target$']
        if endpoint:
            policy['metrics_endpoint'] = endpoint + '/v1/metrics'
            policy['trace_export']['endpoint'] = endpoint + '/v1/traces'
        else:
            delegates = Capture(), TraceCapture()
        runtime = Runtime(policy, *(delegates if not endpoint else ()))
        monitor = Monitor(policy, runtime)
        try:
            monitor.install()
            measured = namespace['run'](runtime.provider.force_flush if flush else lambda: None)
        finally:
            monitor.close()
            before_close = random.getstate()
            runtime.close()
        self.assertEqual(before_close, random.getstate(), 'shutdown consumed application RNG')
        self.assertEqual(baseline, measured)
        return runtime

    def test_monitor_and_exemplars_preserve_seeded_values_and_state(self):
        for filter_name in ('always_off', 'always_on'):
            with patch.dict(os.environ, {'OTEL_METRICS_EXEMPLAR_FILTER': filter_name}):
                for metrics, traces in ((False, False), (True, False), (False, True), (True, True)):
                    with self.subTest(filter=filter_name, metrics=metrics, traces=traces):
                        self.exercise(metrics, traces)

    def test_real_successful_and_failed_exports_preserve_application_rng(self):
        for status in (200, 503):
            with self.subTest(status=status), receiver([(status, b'')]) as (endpoint, requests):
                self.exercise(True, True, flush=True, endpoint=endpoint)
                self.assertTrue(requests)

    def test_ids_exemplars_and_external_provider_remain_independent(self):
        original = random.getstate()
        with patch.dict(os.environ, {'OTEL_METRICS_EXEMPLAR_FILTER': 'trace_based'}):
            policy = trace_plan()
            metrics, traces = Capture(), TraceCapture()
            runtime = Runtime(policy, metrics, traces)
            context = SpanContext(123, 456, False, TraceFlags(1))
            token = attach(set_span_in_context(NonRecordingSpan(context), Context()))
            try:
                for number in range(8):
                    runtime.enter(number, 'target')
                    self.assertEqual(3, runtime.pending[number].trace.context.trace_flags)
                    runtime.exit(number, False)
                runtime.provider.force_flush()
            finally:
                detach(token)
                runtime.close()
        self.assertEqual(original, random.getstate())
        points = {metric.name: metric.data.data_points for batch in metrics.batches
                  for resource in batch.resource_metrics for scope in resource.scope_metrics
                  for metric in scope.metrics}
        self.assertEqual(8, points['otelc.function.calls'][-1].value)
        self.assertTrue(points['otelc.function.calls'][0].exemplars)
        self.assertTrue(points['otelc.function.duration'][0].exemplars)
        for name in ('otelc.function.calls', 'otelc.function.duration'):
            for exemplar in points[name][0].exemplars:
                self.assertEqual((123, 456), (exemplar.trace_id, exemplar.span_id))
        spans = traces.spans()
        self.assertEqual(8, len(spans))
        self.assertEqual(8, len({span.trace_id for span in spans}))
        self.assertTrue(all(len(span.trace_id) == 16 and len(span.span_id) == 8
                            for span in spans))
        # An unrelated provider retains its ordinary generator and global state contract.
        random.seed(73)
        expected = random.getrandbits(64)
        random.seed(73)
        self.assertEqual(expected, RandomIdGenerator().generate_span_id())

    def test_owned_jitter_handles_connection_and_status_retries_without_global_random(self):
        for failures in ('connection', 'status'):
            response = requests.Response()
            response.status_code = 200
            response.raw = Mock()
            response.raw.read.return_value = b''
            retry = requests.Response()
            retry.status_code = 503
            retry.raw = Mock()
            attempts = ([requests.ConnectionError(), requests.ConnectionError(), response]
                        if failures == 'connection' else [retry, response])
            from quux_otelc_python.telemetry import Session
            original = random.getstate()
            with patch.object(requests.Session, 'request', side_effect=attempts) as post, \
                    patch('quux_otelc_python.telemetry.time.sleep'):
                session = Session()
                try:
                    self.assertEqual(200, session.post('http://127.0.0.1/v1/metrics', timeout=5).status_code)
                finally:
                    session.close()
                self.assertEqual(len(attempts), post.call_count)
            self.assertEqual(original, random.getstate())

    def test_private_generators_reject_invalid_draws_and_reservoir_resets(self):
        generator = PrivateRandom()
        draws = iter((7, 3))
        generator.bits = lambda width: next(draws)
        self.assertEqual(3, generator.below(5))
        draws = iter((0, 42))
        self.assertEqual(42, generator.nonzero(64))
        identifiers = PrivateIdGenerator()
        self.assertGreater(identifiers.generate_span_id(), 0)
        self.assertGreater(identifiers.generate_trace_id(), 0)
        self.assertTrue(identifiers.is_trace_id_random())
        reservoir = PrivateReservoir(size=1)
        self.assertEqual(0, reservoir._find_bucket_index(1, 0, {}, Context()))
        reservoir.random.below = lambda limit: 0
        self.assertEqual(0, reservoir._find_bucket_index(1, 0, {}, Context()))
        reservoir.random.below = lambda limit: 1
        with self.assertRaises(BucketIndexError):
            reservoir._find_bucket_index(1, 0, {}, Context())
        reservoir.collect({})
        self.assertEqual(0, reservoir._find_bucket_index(1, 0, {}, Context()))
        views = metric_views([0.001, 1.0])
        first = views[0]._exemplar_reservoir_factory(None)()
        second = views[1]._exemplar_reservoir_factory(None)()
        self.assertIsNot(first, second)
        self.assertIs(first.random, second.random)
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            isolate_exporter(type('ForeignExporter', (), {'_client': object()})())

    @unittest.skipUnless((ROOT / 'target/debug/quux-otelc').is_file(), 'native CLI required')
    def test_public_cli_keeps_seeded_output_with_periodic_real_exports(self):
        application = '''import json, random, time
random.seed(12345)
def target():
    return random.random()
values = []
for _ in range(5):
    values.append(target())
    time.sleep(0.04)
print(json.dumps([values, random.getstate()]))
'''
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('OTEL_', 'OTELC_'))}
        environment['OTEL_METRICS_EXEMPLAR_FILTER'] = 'always_on'
        with tempfile.TemporaryDirectory() as name, receiver() as (endpoint, received):
            root = Path(name)
            source, config = root / 'entry.py', root / 'policy.toml'
            source.write_text(application)
            for metrics, traces in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(metrics=metrics, traces=traces):
                    config.write_text('schema_version=2\nlanguages=["python"]\n[sources]\ninclude=["entry.py"]\n'
                                      '[functions]\ninclude=["entry.target"]\n'
                                      f'[metrics]\nenabled={str(metrics).lower()}\n[traces]\nenabled={str(traces).lower()}\nroot_sample_ratio=1.0\n'
                                      f'[export]\nendpoint={json.dumps(endpoint)}\ninterval_ms=20\ntimeout_ms=300\n')
                    config_bytes = config.read_bytes()
                    received.clear()
                    baseline = subprocess.run([sys.executable, source.name], cwd=root, env=environment,
                                              capture_output=True, timeout=15)
                    measured = subprocess.run([str(ROOT / 'target/debug/quux-otelc'), '--config', str(config),
                                               'python', source.name], cwd=root,
                                              env=dict(environment, OTELC_PYTHON=sys.executable,
                                                       OTELC_ADAPTER_ROOT=str(ROOT / 'adapters')),
                                              capture_output=True, timeout=15)
                    self.assertEqual((baseline.returncode, baseline.stdout, baseline.stderr),
                                     (measured.returncode, measured.stdout, measured.stderr))
                    self.assertEqual(application, source.read_text())
                    self.assertEqual(config_bytes, config.read_bytes())
                    if metrics or traces:
                        self.assertGreater(len(received), 1)
