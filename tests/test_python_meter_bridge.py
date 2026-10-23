"""Library instruments use actual live gates and the separate SDK exporter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otelc import python_sdk_protocol as wire
from otelc.config import Policy
from otelc.python_meter_bridge import BridgeMeterProvider
from otelc.python_runtime import PythonRuntime
from otelc.python_sdk_process import SdkProcess
from test_java_integration import Capture


class PythonMeterBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.policy = Policy.parse({'schema_version': 3,
            'metrics': {'collection': False, 'emission': False},
            'traces': {'enabled': True, 'sample_ratio': 1.0},
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 100000}})
        self.sdk = SdkProcess.start(self.policy, self.root / 'sdk')
        self.addCleanup(self.sdk.abort)
        self.runtime = PythonRuntime(self.policy, self.sdk.channel)
        self.addCleanup(self.runtime.close)
        self.provider = BridgeMeterProvider(self.runtime)

    def finish(self):
        self.runtime.close()
        return self.sdk.wait_report()

    def test_cached_instruments_keep_scope_and_metadata_through_all_four_live_states(self) -> None:
        meter = self.provider.get_meter('official.http', '0.66b1', 'https://schema.invalid', {'role': 'client'})
        self.assertIs(meter, self.provider.get_meter('official.http', '0.66b1', 'https://schema.invalid', {'role': 'client'}))
        counter = meter.create_counter('http.requests', '{request}', 'Requests')
        self.assertEqual((counter.name, counter.unit, counter.description), ('http.requests', '{request}', 'Requests'))
        self.assertIs(counter, meter.create_counter('http.requests', '{request}', 'Requests'))
        duration = meter.create_histogram('http.duration', 's', 'Duration', explicit_bucket_boundaries_advisory=(.01, .1))
        active = meter.create_up_down_counter('http.active', '{request}', 'Active')
        gauge = meter.create_gauge('http.size', 'By', 'Size')
        for collection, emission in ((False, False), (True, False), (False, True), (True, True)):
            self.runtime.controls(collection, emission)
            with self.runtime._provider.get_tracer('fixture').start_as_current_span('request'):
                counter.add(1, {'method': 'GET', 'tags': ('a', 'b')})
                duration.record(.02, {'method': 'GET'})
                active.add(-1)
                gauge.set(123)
            self.sdk.channel.request('flush')
        self.assertEqual(self.capture.points('http.requests')[-1].as_int, 2)
        histogram = self.capture.points('http.duration')[-1]
        self.assertEqual((histogram.count, list(histogram.explicit_bounds)), (2, [.01, .1]))
        self.assertTrue(histogram.exemplars)
        self.assertEqual(self.capture.points('http.active')[-1].as_int, -2)
        self.assertEqual(self.capture.points('http.size')[-1].as_int, 123)
        scopes = [scope for request in self.capture.metrics for resource in request.resource_metrics
                  for scope in resource.scope_metrics if scope.scope.name == 'official.http']
        self.assertTrue(scopes)
        self.assertTrue(all(scope.scope.version == '0.66b1' and scope.schema_url == 'https://schema.invalid' for scope in scopes))
        metric = next(metric for scope in scopes for metric in scope.metrics if metric.name == 'http.requests')
        self.assertEqual((metric.unit, metric.description), ('{request}', 'Requests'))
        report = self.finish()
        self.assertEqual(report['phase'], 'closed')
        self.assertEqual(report['span_accounting']['sdk_exported'], 4)

    def test_invalid_values_do_not_invoke_object_hooks_or_break_later_measurements(self) -> None:
        class Metadata(type):
            def __eq__(cls, other):
                raise AssertionError('must not compare application types')

        class Opaque(metaclass=Metadata):
            def __str__(self):
                raise AssertionError('must not format metric values')

            def __iter__(self):
                raise AssertionError('must not iterate opaque attribute mappings')

        self.runtime.controls(True, True)
        counter = self.provider.get_meter('official').create_counter('http.requests')
        for amount, labels in ((-1, {}), (1, Opaque()), (Opaque(), {}), (1, {'opaque': Opaque()})):
            counter.add(amount, labels)
        counter.add(2)
        self.sdk.channel.request('flush')
        self.assertEqual(self.capture.points('http.requests')[-1].as_int, 2)
        self.assertEqual(self.runtime.losses['library_metric'], 4)
        self.assertEqual(self.finish()['phase'], 'failed')

    def test_capacity_bounds_all_meters_and_records_unknown_callbacks_as_loss(self) -> None:
        callback = mock.Mock()
        with mock.patch.object(wire, 'MAX_INSTRUMENTS', 2):
            first = self.provider.get_meter('one')
            second = self.provider.get_meter('two')
            self.provider.get_meter('overflow')
            first.create_counter('one')
            second.create_counter('two')
            first.create_counter('overflow')
        for kind in ('counter', 'up_down_counter', 'gauge'):
            getattr(first, 'create_observable_' + kind)('observed.' + kind, [callback])
        callback.assert_not_called()
        self.assertEqual(len(self.provider._meters), 2)
        self.assertEqual(len(self.provider._instruments), 2)
        self.assertEqual(self.runtime.losses, {'library_meter': 1, 'library_instrument': 1,
                                             'library_observable_unsupported': 3})
        self.assertEqual(self.finish()['phase'], 'failed')

    def test_invalid_metadata_and_histogram_boundaries_are_explicit_losses(self) -> None:
        for version in (False, 0):
            self.provider.get_meter('official', version=version)
        meter = self.provider.get_meter('official')
        for boundaries in ((1, 1), (-1,), (float('inf'),), tuple(range(161))):
            meter.create_histogram('http.duration', explicit_bucket_boundaries_advisory=boundaries)
        self.assertEqual(self.runtime.losses, {'library_meter': 2, 'library_instrument': 4})
        self.assertEqual(self.finish()['phase'], 'failed')

    def test_closed_runtime_keeps_cached_instruments_inert(self) -> None:
        counter = self.provider.get_meter('official').create_counter('http.requests')
        self.runtime.controls(True, True)
        self.assertEqual(self.finish()['phase'], 'closed')
        before = self.sdk.channel.writer.snapshot()
        counter.add(1)
        self.assertEqual(self.sdk.channel.writer.snapshot(), before)
