"""Live metric gates exercise the actual official Python SDK and readers."""

from __future__ import annotations

import concurrent.futures
import threading
import unittest
from collections.abc import Callable
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import AlwaysOnExemplarFilter, MeterProvider
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
    MetricsData,
    PeriodicExportingMetricReader,
)

from otelc.python_metrics import (
    GatedMeterProvider,
    GatedMetricExporter,
    MetricControls,
)


def points(data: MetricsData | None) -> dict[str, list[Any]]:
    """Read SDK output, rather than asserting calls to wrapper methods."""
    if data is None:
        return {}
    return {
        metric.name: list(metric.data.data_points)
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


class RecordingExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[MetricsData] = []
        self.result = MetricExportResult.SUCCESS
        self.export_error = False
        self.flush_result = True
        self.flush_error = False
        self.shutdown_error = False
        self.shutdown_calls = 0
        self.shutdown_budget = 0.
        self.export_hook: Callable[[], None] = lambda: None
        self.shutdown_hook: Callable[[], None] = lambda: None

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000,
               **kwargs: Any) -> MetricExportResult:
        self.batches.append(metrics_data)
        self.export_hook()
        if self.export_error:
            raise OSError('controlled export failure')
        return self.result

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        if self.flush_error:
            raise OSError('controlled flush failure')
        return self.flush_result

    def shutdown(self, timeout_millis: float = 30_000,
                 **kwargs: Any) -> None:
        self.shutdown_calls += 1
        self.shutdown_budget = timeout_millis
        self.shutdown_hook()
        if self.shutdown_error:
            raise OSError('controlled shutdown failure')


class PythonMetricGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controls = MetricControls(False, False)
        self.reader = InMemoryMetricReader()
        self.sdk = MeterProvider(
            metric_readers=[self.reader], shutdown_on_exit=False,
            exemplar_filter=AlwaysOnExemplarFilter(),
        )
        self.addCleanup(self.sdk.shutdown)
        self.provider = GatedMeterProvider(self.sdk, self.controls)
        self.meter = self.provider.get_meter(
            'ordinary-library', '1.2', 'https://example.test/schema',
            {'library.role': 'test'},
        )

    def test_sync_wrappers_preserve_actual_sdk_public_instrument_metadata(self) -> None:
        sdk_meter = self.sdk.get_meter('ordinary-library', '1.2', 'https://example.test/schema',
                                       {'library.role': 'test'})
        for kind in ('counter', 'up_down_counter', 'histogram', 'gauge'):
            with self.subTest(kind=kind):
                create = getattr(self.meter, 'create_' + kind)
                raw_create = getattr(sdk_meter, 'create_' + kind)
                options = {'explicit_bucket_boundaries_advisory': [.1, .5, 1.]} if kind == 'histogram' else {}
                wrapped = create('original_' + kind, 's', 'Original description', **options)
                raw = raw_create('original_' + kind, 's', 'Original description', **options)
                for field in ('name', 'unit', 'description', 'instrumentation_scope'):
                    self.assertEqual(getattr(wrapped, field), getattr(raw, field))
                self.assertIs(wrapped.instrumentation_scope, raw.instrumentation_scope)
                self.assertIs(wrapped, create('original_' + kind, 's', 'Original description', **options))
        self.controls.update(collection=True, emission=True)
        self.meter.create_histogram('original_histogram', 's', 'Original description',
            explicit_bucket_boundaries_advisory=[.1, .5, 1.]).record(.25)
        data = self.reader.get_metrics_data()
        histogram = points(data)['original_histogram'][0]
        self.assertEqual(tuple(histogram.explicit_bounds), (.1, .5, 1.))
        metric = next(metric for resource in data.resource_metrics for scope in resource.scope_metrics
                      for metric in scope.metrics if metric.name == 'original_histogram')
        self.assertEqual((metric.unit, metric.description), ('s', 'Original description'))

    def test_cached_sync_instruments_enable_disable_and_keep_sdk_semantics(self) -> None:
        counter = self.meter.create_counter('calls', '{call}', 'Call count')
        up_down = self.meter.create_up_down_counter('active')
        histogram = self.meter.create_histogram(
            'duration', 's', explicit_bucket_boundaries_advisory=[.1, .5, 1.],
        )
        gauge = self.meter.create_gauge('depth')
        self.assertIs(counter, self.meter.create_counter('calls', '{call}', 'Call count'))
        self.assertIsInstance(counter, metrics.Counter)
        self.assertIsInstance(up_down, metrics.UpDownCounter)
        self.assertIsInstance(histogram, metrics.Histogram)
        self.assertIsInstance(gauge, metrics._Gauge)
        counter.add(100)
        up_down.add(100)
        histogram.record(100)
        gauge.set(100)
        self.assertEqual(points(self.reader.get_metrics_data()), {})

        context = trace.set_span_in_context(trace.NonRecordingSpan(
            trace.SpanContext(0x1234567890, 0x1234, False, trace.TraceFlags(1)),
        ))
        attributes = {'phase': 'retained'}
        self.controls.update(collection=True)
        counter.add(3, attributes, context)
        up_down.add(-2, attributes, context)
        histogram.record(.25, attributes, context)
        gauge.set(7, attributes, context)
        self.controls.update(collection=False, emission=True)
        counter.add(100, attributes)
        up_down.add(100, attributes)
        histogram.record(100, attributes)
        gauge.set(100, attributes)
        data = self.reader.get_metrics_data()
        observed = points(data)
        self.assertEqual(observed['calls'][0].value, 3)
        self.assertEqual(observed['active'][0].value, -2)
        self.assertEqual(observed['depth'][0].value, 7)
        duration = observed['duration'][0]
        self.assertEqual((duration.count, duration.sum), (1, .25))
        self.assertEqual(tuple(duration.explicit_bounds), (.1, .5, 1.))
        self.assertEqual(duration.exemplars[0].span_id, 0x1234)
        self.assertEqual(duration.exemplars[0].trace_id, 0x1234567890)
        self.assertTrue(all(row[0].attributes == attributes for row in observed.values()))
        scope = data.resource_metrics[0].scope_metrics[0].scope
        self.assertEqual((scope.name, scope.version, scope.schema_url),
                         ('ordinary-library', '1.2', 'https://example.test/schema'))
        self.assertEqual(scope.attributes, {'library.role': 'test'})

        self.controls.update(collection=True)
        counter.add(4, attributes)
        up_down.add(3, attributes)
        histogram.record(.5, attributes)
        gauge.set(9, attributes)
        observed = points(self.reader.get_metrics_data())
        self.assertEqual(observed['calls'][0].value, 7)
        self.assertEqual(observed['active'][0].value, 1)
        self.assertEqual(observed['depth'][0].value, 9)
        self.assertEqual((observed['duration'][0].count,
                          observed['duration'][0].sum), (2, .75))

    def test_observables_and_generator_setup_stay_inactive_while_off(self) -> None:
        calls = []
        factories = (
            (self.meter.create_observable_counter, 'count', 5),
            (self.meter.create_observable_up_down_counter, 'signed', -2),
            (self.meter.create_observable_gauge, 'current', 9),
        )
        for create, name, value in factories:
            def callback(options, name=name, value=value):
                calls.append((name, options.timeout_millis))
                return [metrics.Observation(value, {'kind': name})]
            create(name, [callback])
        events = []

        def generator():
            events.append('setup')
            options = yield
            for value in (12, 13):
                events.append(options.timeout_millis)
                options = yield [metrics.Observation(value)]

        self.meter.create_observable_counter('generated', [generator()])
        self.assertEqual(events, [])
        self.assertEqual(points(self.reader.get_metrics_data()), {})
        self.assertEqual((calls, events), ([], []))
        self.controls.update(collection=True)
        first = points(self.reader.get_metrics_data())
        self.assertEqual({name: rows[0].value for name, rows in first.items()},
                         {'count': 5, 'signed': -2, 'current': 9, 'generated': 12})
        self.assertEqual(len(calls), 3)
        self.assertEqual(events[0], 'setup')
        self.controls.update(collection=False)
        self.reader.get_metrics_data()
        self.assertEqual((len(calls), len(events)), (3, 2))
        self.controls.update(collection=True)
        second = points(self.reader.get_metrics_data())
        self.assertEqual(second['generated'][0].value, 13)
        third = points(self.reader.get_metrics_data())
        self.assertNotIn('generated', third)
        self.assertEqual(len(calls), 9)
        self.assertTrue(all(timeout > 0 for _, timeout in calls))

    def test_callback_crossing_off_on_cannot_publish_a_late_observation(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        attempts = []

        def callback(_):
            attempts.append(1)
            if len(attempts) == 1:
                entered.set()
                self.assertTrue(release.wait(3), 'test callback not released')
                return [metrics.Observation(99)]
            return [metrics.Observation(5)]

        self.meter.create_observable_gauge('value', [callback])
        self.controls.update(collection=True)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(self.reader.get_metrics_data)
            try:
                self.assertTrue(entered.wait(3))
                self.controls.update(collection=False)
                self.controls.update(collection=True)
            finally:
                release.set()
            self.assertEqual(points(pending.result(timeout=3)), {})
        self.assertEqual(points(self.reader.get_metrics_data())['value'][0].value, 5)

    def test_emission_switch_during_callback_does_not_cancel_collection(self) -> None:
        def callback(_):
            self.controls.update(emission=True)
            return [metrics.Observation(6)]

        self.meter.create_observable_gauge('value', [callback])
        self.controls.update(collection=True)
        self.assertEqual(points(self.reader.get_metrics_data())['value'][0].value, 6)

    def test_observable_iterator_cannot_add_points_after_a_collection_transition(self) -> None:
        def callback(_):
            yield metrics.Observation(2, {'point': 'before'})
            self.controls.update(collection=False)
            self.controls.update(collection=True)
            yield metrics.Observation(99, {'point': 'late'})

        self.meter.create_observable_gauge('stream', [callback])
        self.controls.update(collection=True)
        observed = points(self.reader.get_metrics_data())['stream']
        self.assertEqual([(point.value, point.attributes) for point in observed],
                         [(2, {'point': 'before'})])

    def test_scope_wrappers_are_stable_under_concurrent_lookup(self) -> None:
        def get(_):
            return self.provider.get_meter('shared', attributes={'role': 'same'})

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            meters = list(pool.map(get, range(32)))
        self.assertTrue(all(meter is meters[0] for meter in meters))
        other = self.provider.get_meter('shared', attributes={'role': 'other'})
        self.assertIsNot(other, meters[0])
        self.controls.update(collection=True)
        meters[0].create_counter('same').add(2)
        other.create_counter('same').add(3)
        data = self.reader.get_metrics_data()
        scopes = data.resource_metrics[0].scope_metrics
        self.assertEqual(sorted((scope.scope.attributes['role'],
                                 scope.metrics[0].data.data_points[0].value)
                                for scope in scopes), [('other', 3), ('same', 2)])

    def test_invalid_switch_values_leave_both_flags_and_generation_unchanged(self) -> None:
        for collection, emission in ((None, False), (False, 1)):
            with self.assertRaises(ValueError):
                MetricControls(collection, emission)
        before = self.controls.snapshot()
        for fields in ({'collection': 1}, {'emission': 'on'},
                       {'collection': True, 'emission': 0}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.controls.update(**fields)
            self.assertEqual(self.controls.snapshot(), before)
        self.controls.update(emission=True)
        self.assertEqual(self.controls.snapshot().generation, before.generation)
        self.controls.update(collection=True)
        changed = self.controls.snapshot()
        self.controls.update(collection=True)
        self.assertEqual(self.controls.snapshot(), changed)


class PythonMetricExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controls = MetricControls(True, True)
        self.delegate = RecordingExporter()
        self.exporter = GatedMetricExporter(self.delegate, self.controls)
        reader = InMemoryMetricReader()
        sdk = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
        sdk.get_meter('fixture').create_counter('calls').add(1)
        self.data = reader.get_metrics_data()
        sdk.shutdown()

    def test_actual_periodic_reader_cannot_hide_failure_or_discard_suppressed_snapshot(self) -> None:
        self.controls.update(collection=False, emission=False)
        reader = PeriodicExportingMetricReader(
            self.exporter, export_interval_millis=float('inf'),
            export_timeout_millis=500,
        )
        sdk = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
        self.addCleanup(sdk.shutdown, timeout_millis=2000)
        self.addCleanup(self.controls.update, collection=False, emission=False)
        meter = GatedMeterProvider(sdk, self.controls).get_meter('real-sdk')
        counter = meter.create_counter('calls')
        counter.add(100)
        self.assertTrue(reader.force_flush())
        self.assertEqual(self.delegate.batches, [])
        self.controls.update(collection=True)
        counter.add(2)
        self.assertTrue(reader.force_flush())
        self.assertEqual(self.delegate.batches, [])
        self.controls.update(collection=False, emission=True)
        self.assertTrue(reader.force_flush())
        self.assertEqual(self.delegate.batches, [])
        self.controls.update(collection=True)
        self.delegate.result = MetricExportResult.FAILURE
        self.assertTrue(reader.force_flush())  # SDK return is not delivery evidence.
        self.assertEqual(self.exporter.status()['export_failed'], 1)
        self.delegate.result = MetricExportResult.SUCCESS
        counter.add(3)
        self.assertTrue(reader.force_flush())
        self.assertEqual([points(data)['calls'][0].value
                          for data in self.delegate.batches], [2, 5])
        self.assertEqual(self.exporter.status()['export_succeeded'], 1)
        self.assertEqual(self.exporter.status()['in_flight_exports'], 0)
        self.delegate.flush_result = False
        self.assertTrue(reader.force_flush())  # The SDK also ignores this false.
        self.assertEqual(self.exporter.status()['flush_failed'], 1)

    def test_export_failure_and_exception_do_not_poison_later_recovery(self) -> None:
        self.delegate.result = MetricExportResult.FAILURE
        self.assertIs(self.exporter.export(self.data), MetricExportResult.FAILURE)
        self.delegate.export_error = True
        with self.assertRaisesRegex(OSError, 'controlled export'):
            self.exporter.export(self.data)
        self.delegate.export_error = False
        self.delegate.result = MetricExportResult.SUCCESS
        self.assertIs(self.exporter.export(self.data), MetricExportResult.SUCCESS)
        status = self.exporter.status()
        self.assertEqual((status['export_started'], status['export_failed'],
                          status['export_succeeded'], status['in_flight_exports']),
                         (3, 2, 1, 0))
        self.delegate.flush_error = True
        with self.assertRaisesRegex(OSError, 'controlled flush'):
            self.exporter.force_flush()
        self.assertEqual(self.exporter.status()['flush_failed'], 1)
        self.assertEqual(self.exporter.status()['in_flight_flushes'], 0)

    def test_emission_off_reports_admitted_export_until_it_finishes(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def block():
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test export not released')

        self.delegate.export_hook = block
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.exporter.export, self.data)
            try:
                self.assertTrue(entered.wait(3))
                pool.submit(self.controls.update, emission=False).result(timeout=1)
                status = self.exporter.status()
                self.assertEqual(status['in_flight_exports'], 1)
                self.assertFalse(status['effective_emission_off'])
                self.assertIs(self.exporter.export(self.data), MetricExportResult.SUCCESS)
                self.assertEqual(len(self.delegate.batches), 1)
            finally:
                release.set()
            self.assertIs(pending.result(timeout=3), MetricExportResult.SUCCESS)
        self.assertTrue(self.exporter.status()['effective_emission_off'])
        self.assertEqual(self.exporter.status()['export_started'], 1)

    def test_concurrent_shutdown_waits_and_calls_delegate_once(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def block():
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test shutdown not released')

        self.delegate.shutdown_hook = block
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(self.exporter.shutdown)
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(self.exporter.status()['shutdown_complete'])
                with self.assertRaises(TimeoutError):
                    self.exporter.shutdown(timeout_millis=1)
                self.assertIs(self.exporter.export(self.data), MetricExportResult.FAILURE)
            finally:
                release.set()
            pending.result(timeout=3)
        self.exporter.shutdown()
        self.assertEqual(self.delegate.shutdown_calls, 1)
        self.assertTrue(self.exporter.status()['shutdown_complete'])

    def test_failed_shutdown_is_retained_without_reopening_export_admission(self) -> None:
        self.delegate.shutdown_error = True
        with self.assertRaisesRegex(OSError, 'controlled shutdown'):
            self.exporter.shutdown()
        with self.assertRaisesRegex(RuntimeError, 'shutdown failed'):
            self.exporter.shutdown()
        self.assertEqual(self.delegate.shutdown_calls, 1)
        self.assertTrue(self.exporter.status()['shutdown_failed'])
        self.assertIs(self.exporter.export(self.data), MetricExportResult.FAILURE)

    def test_real_reader_shutdown_preserves_its_remaining_timeout_budget(self) -> None:
        reader = PeriodicExportingMetricReader(
            self.exporter, export_interval_millis=float('inf'),
        )
        sdk = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
        sdk.shutdown(timeout_millis=500)
        self.assertEqual(self.delegate.shutdown_calls, 1)
        self.assertGreater(self.delegate.shutdown_budget, 0)
        self.assertLessEqual(self.delegate.shutdown_budget, 500)
