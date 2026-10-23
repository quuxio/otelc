"""Real SDK aggregation and batch processing retain failure and drain facts."""

from __future__ import annotations

import threading
import time
import unittest

from opentelemetry.sdk.metrics.export import MetricExportResult
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from otelc.config import Policy
from otelc.python_sdk_pipeline import SdkPipeline, span_context
from test_python_metrics import RecordingExporter, points
from test_python_sdk_protocol import context, metric, span


class RecordingSpans(SpanExporter):
    def __init__(self) -> None:
        self.spans = []
        self.result = SpanExportResult.SUCCESS
        self.export_error = False
        self.shutdown_error = False
        self.closed = 0

    def export(self, spans) -> SpanExportResult:
        self.spans.extend(spans)
        if self.export_error:
            raise OSError('controlled span export error')
        return self.result

    def shutdown(self) -> None:
        self.closed += 1
        if self.shutdown_error:
            raise OSError('controlled span shutdown error')


class PythonSdkPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metric_exporter = RecordingExporter()
        self.span_exporter = RecordingSpans()
        self.policy = Policy.parse({'schema_version': 3,
            'export': {'interval_ms': 100000, 'timeout_ms': 1000},
            'metrics': {'collection': False, 'emission': False},
            'traces': {'enabled': True, 'sample_ratio': 1.0}})
        self.sdk = SdkPipeline(self.policy, '1' * 32, 123,
            metric_exporter=self.metric_exporter, span_exporter=self.span_exporter)
        self.addCleanup(self.close)

    def close(self) -> None:
        self.metric_exporter.result = MetricExportResult.SUCCESS
        self.metric_exporter.flush_result = True
        self.metric_exporter.export_error = self.metric_exporter.shutdown_error = False
        self.span_exporter.result = SpanExportResult.SUCCESS
        self.span_exporter.export_error = self.span_exporter.shutdown_error = False
        if self.sdk.snapshot()['phase'] == 'running':
            self.sdk.shutdown()

    def test_four_metric_states_and_independent_spans_use_actual_sdk_components(self) -> None:
        for index, (collection, emission) in enumerate(((False, False), (True, False),
                                                       (False, True), (True, True))):
            self.sdk.controls.update(collection=collection, emission=emission)
            self.sdk.metric(metric())
            self.sdk.metric(metric(name='otelc.function.duration', instrument='histogram',
                                   unit='s', value=.002))
            self.sdk.metric(metric(name='depth', instrument='gauge', value=2))
            self.sdk.metric(metric(name='active', instrument='up_down_counter', value=-1))
            self.sdk.span(span(context=context(span_id=f'{index + 2:016x}')))
            self.sdk.flush()
            self.assertEqual(len(self.span_exporter.spans), index + 1)
            self.assertEqual(len(self.metric_exporter.batches), int(collection and emission))
        final = points(self.metric_exporter.batches[-1])
        self.assertEqual(final['otelc.function.calls'][0].value, 2)
        self.assertEqual((final['otelc.function.duration'][0].count,
                          final['otelc.function.duration'][0].sum), (2, .004))
        self.assertEqual(final['depth'][0].value, 2)
        self.assertEqual(final['active'][0].value, -2)
        self.assertEqual(len(self.span_exporter.spans), 4)
        self.assertEqual(self.sdk.snapshot()['span_accounting']['queue_evidence'], 'unavailable')
        self.sdk.shutdown()
        self.sdk.shutdown()
        result = self.sdk.snapshot()
        self.assertEqual(result['phase'], 'closed')
        self.assertEqual(result['span_accounting']['queue_dropped'], 0)
        self.assertEqual(result['span_accounting']['queue_pending'], 0)
        self.assertEqual(result['traces']['exported_items'], 4)
        self.assertEqual((self.metric_exporter.shutdown_calls, self.span_exporter.closed), (1, 1))
        for operation in (lambda: self.sdk.metric(metric()),
                          lambda: self.sdk.span(span()), self.sdk.flush):
            with self.assertRaises(RuntimeError):
                operation()

    def test_readable_span_preserves_original_context_metadata_events_and_links(self) -> None:
        value = span(parent=context(span_id='3'.zfill(16), remote=True,
                                    trace_state='vendor=value'),
                     attributes={'function': 'α'}, scope='library', version='2',
                     scope_attributes={'role': 'source'},
                     events=[{'name': 'event', 'timestamp': 1500, 'attributes': {'value': 4}}],
                     links=[{'context': context(trace_id='4'.zfill(32)), 'attributes': {'link': True}}],
                     status='ERROR', description='Literal', span_kind='SERVER',
                     dropped_attributes=2, dropped_events=3, dropped_links=4)
        self.sdk.span(value)
        self.sdk.flush()
        observed = self.span_exporter.spans[0]
        self.assertEqual(observed.context.trace_id, 1)
        self.assertEqual(observed.context.span_id, 2)
        self.assertEqual(observed.parent.span_id, 3)
        self.assertTrue(observed.parent.is_remote)
        self.assertEqual(observed.parent.trace_state.to_header(), 'vendor=value')
        self.assertEqual((observed.dropped_attributes, observed.dropped_events, observed.dropped_links), (2, 3, 4))
        self.assertEqual(observed.events[0].attributes, {'value': 4})
        self.assertEqual(observed.links[0].context.trace_id, 4)
        self.assertEqual(observed.kind.name, 'SERVER')
        self.assertEqual((observed.status.status_code.name, observed.status.description), ('ERROR', 'Literal'))
        self.assertEqual((observed.start_time, observed.end_time), (1000, 2000))
        self.assertEqual(observed.resource.attributes['process.pid'], 123)
        self.assertEqual(observed.instrumentation_scope.attributes, {'role': 'source'})
        self.assertIsNone(span_context(None))
        invalid = context(trace_state='invalid')
        with self.assertRaises(ValueError):
            span_context(invalid)

    def test_export_failure_is_not_a_flush_success_and_recovery_keeps_history(self) -> None:
        self.sdk.controls.update(collection=True, emission=True)
        self.sdk.metric(metric(value=2))
        self.metric_exporter.result = MetricExportResult.FAILURE
        with self.assertRaises(RuntimeError):
            self.sdk.flush()
        self.assertEqual(self.sdk.snapshot()['export_failed'], 1)
        self.assertEqual(self.sdk.snapshot()['metric_collection']['failed'], 1)
        self.metric_exporter.result = MetricExportResult.SUCCESS
        self.sdk.metric(metric(value=3))
        self.sdk.flush()
        self.assertEqual(points(self.metric_exporter.batches[-1])['otelc.function.calls'][0].value, 5)
        self.assertEqual(self.sdk.snapshot()['export_failed'], 1)
        self.span_exporter.result = SpanExportResult.FAILURE
        self.sdk.span(span())
        with self.assertRaises(RuntimeError):
            self.sdk.flush()
        self.assertEqual(self.sdk.snapshot()['traces']['failed'], 1)
        self.span_exporter.result = SpanExportResult.SUCCESS
        self.sdk.span(span(context=context(span_id='4'.zfill(16))))
        self.sdk.flush()
        self.sdk.shutdown()
        self.assertEqual(self.sdk.snapshot()['span_accounting']['queue_evidence'], 'unavailable')
        self.assertIsNone(self.sdk.snapshot()['span_accounting']['queue_dropped'])

    def test_link_attribute_drop_count_survives_actual_otlp_encoding(self) -> None:
        from opentelemetry import trace
        from opentelemetry.attributes import BoundedAttributes
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import ReadableSpan
        from otelc import python_sdk_protocol as wire
        from otelc.python_sdk_pipeline import WireSpan

        link = trace.Link(span_context(context(trace_id='4'.zfill(32))),
                          BoundedAttributes(maxlen=1, attributes={'a': 1, 'b': 2}))
        original = ReadableSpan('fixture', context=span_context(context()),
            resource=Resource({}), start_time=1000, end_time=2000, links=[link])
        value = span(links=[{'context': context(trace_id='4'.zfill(32)),
                            'attributes': dict(link.attributes),
                            'dropped_attributes': link.dropped_attributes}])
        wire.span(value)
        rebuilt = WireSpan(value, Resource({}))
        def encoded_link(item):
            return encode_spans([item]).resource_spans[0].scope_spans[0].spans[0].links[0]
        expected, actual = encoded_link(original), encoded_link(rebuilt)
        self.assertEqual(expected.dropped_attributes_count, 1)
        self.assertEqual(actual, expected)
        # Older records retain their explicit default of zero.
        del value['links'][0]['dropped_attributes']
        self.assertEqual(encoded_link(WireSpan(value, Resource({}))).dropped_attributes_count, 0)

    def test_event_attribute_drop_count_is_retained_on_actual_export(self) -> None:
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from otelc import python_sdk_protocol as wire

        value = span(events=[{'name': 'event', 'timestamp': 1500,
                             'attributes': {'b': 2}, 'dropped_attributes': 1}])
        wire.span(value)
        self.sdk.span(value)
        self.sdk.flush()
        raw = encode_spans(self.span_exporter.spans).resource_spans[0].scope_spans[0].spans[0]
        self.assertEqual(raw.events[0].dropped_attributes_count, 1)

    def test_periodic_failures_are_recorded_and_later_collection_can_recover(self) -> None:
        self.sdk.shutdown()
        self.metric_exporter = RecordingExporter()
        self.span_exporter = RecordingSpans()
        data = self.policy.data | {'export': self.policy.data['export'] | {'interval_ms': 10}}
        self.sdk = SdkPipeline(Policy.parse(data), '1' * 32, 123,
            metric_exporter=self.metric_exporter, span_exporter=self.span_exporter)
        ready = threading.Event()
        self.metric_exporter.export_hook = ready.set
        self.metric_exporter.result = MetricExportResult.FAILURE
        self.sdk.controls.update(collection=True, emission=True)
        self.sdk.metric(metric())
        self.assertTrue(ready.wait(2))
        deadline = time.monotonic() + 2
        while not self.sdk.snapshot()['metric_collection']['failed']:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.005)
        self.metric_exporter.result = MetricExportResult.SUCCESS
        self.sdk.flush()
        self.assertGreater(self.sdk.snapshot()['metric_collection']['succeeded'], 0)

    def test_reader_lock_budget_and_closed_lifecycle_are_observable(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def block():
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test export not released')

        def collect():
            try:
                self.sdk._reader.force_flush(timeout_millis=2000)
            except Exception as error:
                errors.append(type(error).__name__)

        self.metric_exporter.export_hook = block
        self.sdk.controls.update(collection=True, emission=True)
        self.sdk.metric(metric())
        thread = threading.Thread(target=collect)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(TimeoutError):
                self.sdk._reader.force_flush(timeout_millis=5)
            self.assertEqual(self.sdk.snapshot()['metric_collection']['pending'], 1)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with self.assertRaises(TimeoutError):
            self.sdk._reader.force_flush(timeout_millis=0)
        self.sdk.shutdown()
        self.sdk._reader.shutdown()
        with self.assertRaises(RuntimeError):
            self.sdk._reader.start()
        with self.assertRaises(RuntimeError):
            self.sdk._reader.force_flush()

    def test_failed_shutdown_stays_failed_and_is_not_retried_as_success(self) -> None:
        self.metric_exporter.shutdown_error = True
        with self.assertRaises(RuntimeError):
            self.sdk.shutdown()
        self.assertEqual(self.sdk.snapshot()['phase'], 'failed')
        with self.assertRaises(RuntimeError):
            self.sdk.shutdown()
        self.assertEqual(self.metric_exporter.shutdown_calls, 1)

    def test_tracing_disabled_or_unsampled_span_is_not_queued(self) -> None:
        unsampled = span(context=context(trace_flags=0))
        with self.assertRaises(ValueError):
            self.sdk.span(unsampled)
        self.sdk._traces_enabled = False
        value = span()
        with self.assertRaises(ValueError):
            self.sdk.span(value)
        self.assertEqual(self.sdk.snapshot()['span_accounting']['received'], 0)
