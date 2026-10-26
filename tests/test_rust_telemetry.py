"""Literal receipt controls cannot invent missing spans, metrics or SDK units."""
from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from ci import rust_telemetry as receipt
from otelc.config import Policy
from test_native_benchmark import drain


class RustTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['rust'],
            'resource': {'service_name': 'literal-receipt'}, 'traces': {'enabled': True, 'sample_ratio': 1.0}})
        self.health = drain()
        self.health.pop('object_accounting')
        self.health['language'] = 'rust'
        self.health['function_accounting'].update(started='2', ended='2')
        traces, metrics = ExportTraceServiceRequest(), ExportMetricsServiceRequest()
        resource = traces.resource_spans.add()
        resource.resource.attributes.add(key='service.name').value.string_value = 'literal-receipt'
        scope = resource.scope_spans.add()
        for index in (1, 2):
            scope.spans.add(name='app.rs::workload', trace_id=bytes([index])*16,
                            span_id=bytes([index])*8)
        self.span_scope = scope
        resource = metrics.resource_metrics.add()
        resource.resource.attributes.add(key='service.name').value.string_value = 'literal-receipt'
        scope = resource.scope_metrics.add()
        calls = scope.metrics.add(name='otelc.function.calls')
        calls.sum.aggregation_temporality = 2
        calls.sum.is_monotonic = True
        point = calls.sum.data_points.add(as_int=2, start_time_unix_nano=1, time_unix_nano=2)
        point.attributes.add(key='function').value.string_value = 'app.rs::workload'
        duration = scope.metrics.add(name='otelc.function.duration')
        duration.histogram.aggregation_temporality = 2
        bounds = self.policy.data['metrics']['histogram_boundaries_seconds']
        point = duration.histogram.data_points.add(count=2, sum=0, min=0, max=0,
            start_time_unix_nano=1, time_unix_nano=2, bucket_counts=[2]+[0]*len(bounds), explicit_bounds=bounds)
        point.attributes.add(key='function').value.string_value = 'app.rs::workload'
        self.metric_scope = scope
        self.capture = SimpleNamespace(traces=[traces], metrics=[metrics])

    def verify(self) -> dict:
        return receipt.verify(self.capture, self.policy, self.health,
                              function='app.rs::workload', expected_spans=2, expected_calls=2)

    def test_actual_point_units_are_separate_from_empty_api_batch_counts(self) -> None:
        self.health['metric_exports'].update(batches_started='3', batches_succeeded='3')
        result = self.verify()
        self.assertEqual(result['received_units'], {'spans': 2, 'metric_points': 2})
        self.assertEqual(result['cumulative_calls'], 2)

    def test_other_services_are_excluded_without_inventing_empty_receipt(self) -> None:
        resource = self.capture.metrics[0].resource_metrics.add()
        resource.resource.attributes.add(key='service.name').value.string_value = 'other'
        resource.scope_metrics.add().metrics.add(name='foreign').summary.SetInParent()
        self.verify()
        self.capture.traces.clear()
        with self.assertRaisesRegex(ValueError, 'root span denominator'):
            self.verify()

    def test_root_identity_parent_and_complete_denominator_cannot_change(self) -> None:
        original = self.capture.traces[0].SerializeToString()
        for changed in ('duplicate', 'parent', 'zero', 'name'):
            self.capture.traces[0].ParseFromString(original)
            spans = self.capture.traces[0].resource_spans[0].scope_spans[0].spans
            if changed == 'duplicate':
                spans[1].trace_id, spans[1].span_id = spans[0].trace_id, spans[0].span_id
            elif changed == 'parent':
                spans[1].parent_span_id = b'p'*8
            elif changed == 'zero':
                spans[1].trace_id = bytes(16)
            else:
                spans[1].name = 'another function'
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.verify()

    def test_metric_names_types_labels_histograms_and_final_values_are_checked(self) -> None:
        original = self.capture.metrics[0].SerializeToString()
        for changed in ('name', 'double', 'calls', 'label', 'count', 'bounds', 'buckets', 'summary', 'gauge', 'no-duration'):
            self.capture.metrics[0].ParseFromString(original)
            metrics = self.capture.metrics[0].resource_metrics[0].scope_metrics[0].metrics
            calls, duration = metrics
            if changed == 'name': calls.name = 'otelc.function.wrong'
            elif changed == 'double': calls.sum.data_points[0].as_double = 2.0
            elif changed == 'calls': calls.sum.data_points[0].as_int = 1
            elif changed == 'label': calls.sum.data_points[0].attributes[0].value.string_value = 'wrong'
            elif changed == 'count': duration.histogram.data_points[0].count = 1
            elif changed == 'bounds': duration.histogram.data_points[0].explicit_bounds[0] = 99
            elif changed == 'buckets': duration.histogram.data_points[0].bucket_counts[0] = 1
            elif changed == 'summary': duration.summary.SetInParent()
            elif changed == 'gauge': duration.gauge.data_points.add(as_int=2)
            else: del metrics[1]
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.verify()

    def test_received_units_must_match_final_sdk_accounting(self) -> None:
        self.health['metric_exports'].update(units_started='3', units_succeeded='3')
        with self.assertRaisesRegex(ValueError, 'SDK unit count'):
            self.verify()

    def test_genuine_loss_or_failed_drain_is_never_accepted(self) -> None:
        self.health['function_accounting']['lost'] = '1'
        with self.assertRaisesRegex(ValueError, 'observation loss'):
            self.verify()

    def test_drop_type_counts_and_links_stay_bound_to_original_roots(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['lifetimes']['enabled'] = True
        policy = Policy.parse(data)
        dropped = drain()['object_accounting']
        dropped['dropping'] = dropped.pop('destroying')
        self.health.update(schema_version=5, drop_accounting=dropped)
        self.health['span_accounting'].update(sampled_started='3', sampled_ended='3')
        self.health['trace_exports'].update(units_started='3', units_succeeded='3')
        self.health['metric_exports'].update(units_started='6', units_succeeded='6')
        bounds = self.policy.data['metrics']['histogram_boundaries_seconds']
        for name, kind in (('admitted', 'sum'), ('completed', 'sum'), ('duration', 'histogram'), ('live', 'gauge')):
            metric = self.metric_scope.metrics.add(name='otelc.lifetime.'+name)
            if kind == 'histogram':
                metric.histogram.aggregation_temporality = 2
                point = metric.histogram.data_points.add(count=1, sum=0, min=0, max=0,
                    start_time_unix_nano=1, time_unix_nano=2, bucket_counts=[1]+[0]*len(bounds), explicit_bounds=bounds)
            else:
                if kind == 'sum':
                    metric.sum.aggregation_temporality, metric.sum.is_monotonic = 2, True
                point = getattr(metric, kind).data_points.add(as_int=0 if name == 'live' else 1,
                    start_time_unix_nano=1, time_unix_nano=2)
            point.attributes.add(key='boundary').value.string_value = 'drop_entry'
            point.attributes.add(key='type').value.string_value = 'Item'
        span = self.span_scope.spans.add(name='Item lifetime', trace_id=b'\1'*16, span_id=b'\3'*8)
        span.links.add(trace_id=b'\1'*16, span_id=b'\1'*8)

        def verify(lifetimes=None):
            return receipt.verify(self.capture, policy, self.health, function='app.rs::workload',
                                  expected_spans=2, expected_calls=2,
                                  lifetimes={'Item lifetime': 1} if lifetimes is None else lifetimes)

        self.assertEqual(verify()['lifetime_spans'], {'Item lifetime': 1})
        self.health['drop_accounting'].update(admitted='2', completed='2')
        with self.assertRaises(ValueError): verify()
        self.health['drop_accounting'].update(admitted='1', completed='1')
        with self.assertRaises(ValueError): verify({'wrong lifetime': 1})
        span.links[0].span_id = b'\4'*8
        with self.assertRaisesRegex(ValueError, 'original function link'): verify()

    def test_lifetime_metrics_cannot_drop_types_values_bounds_or_instruments(self) -> None:
        bounds = self.policy.data['metrics']['histogram_boundaries_seconds']
        metrics = []
        for name, kind in (('admitted', 'sum'), ('completed', 'sum'), ('duration', 'histogram'), ('live', 'gauge')):
            message = ExportMetricsServiceRequest()
            metric = message.resource_metrics.add().scope_metrics.add().metrics.add(name='otelc.lifetime.'+name)
            if kind == 'sum': metric.sum.aggregation_temporality, metric.sum.is_monotonic = 2, True
            if kind == 'histogram': metric.histogram.aggregation_temporality = 2
            point = (metric.histogram.data_points.add(count=1, sum=0, min=0, max=0,
                        start_time_unix_nano=1, time_unix_nano=2,
                        bucket_counts=[1]+[0]*len(bounds), explicit_bounds=bounds)
                     if kind == 'histogram' else getattr(metric, kind).data_points.add(as_int=0 if name == 'live' else 1,
                        start_time_unix_nano=1, time_unix_nano=2))
            point.attributes.add(key='boundary').value.string_value = 'drop_entry'
            point.attributes.add(key='type').value.string_value = 'Item'
            metrics.append((metric.name, kind, point))
        receipt.lifetime_metrics(metrics, {'Item lifetime': 1}, bounds)
        for changed in ('missing', 'wrong-type', 'live', 'double', 'bounds', 'count', 'instrument', 'kind'):
            values = copy.deepcopy(metrics)
            if changed == 'missing': values.pop()
            elif changed == 'wrong-type': values[0][2].attributes[1].value.string_value = 'foreign'
            elif changed == 'live': values[3][2].as_int = 1
            elif changed == 'double': values[0][2].as_double = 1.0
            elif changed == 'bounds': values[2][2].explicit_bounds[0] = 99
            elif changed == 'count': values[2][2].count = 3
            elif changed == 'instrument': values[0] = ('otelc.lifetime.unknown', values[0][1], values[0][2])
            else: values[0] = (values[0][0], 'gauge', values[0][2])
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                receipt.lifetime_metrics(values, {'Item lifetime': 1}, bounds)

    def test_nonfinite_negative_or_impossible_duration_state_refuses(self) -> None:
        original = self.capture.metrics[0].SerializeToString()
        for change in ('nan', 'infinity', 'negative', 'sum', 'bucket-extreme', 'missing-min', 'missing-sum', 'flags'):
            self.capture.metrics[0].ParseFromString(original)
            point = self.capture.metrics[0].resource_metrics[0].scope_metrics[0].metrics[1].histogram.data_points[0]
            if change == 'nan': point.sum = float('nan')
            elif change == 'infinity': point.max = float('inf')
            elif change == 'negative': point.min, point.max, point.sum = -2, -1, -3
            elif change == 'sum': point.sum = 1
            elif change == 'bucket-extreme': point.max, point.sum = 1, 1
            elif change == 'missing-min': point.ClearField('min')
            elif change == 'missing-sum': point.ClearField('sum')
            else: point.flags = 1
            with self.subTest(change=change), self.assertRaises(ValueError): self.verify()

    def test_exact_declared_roots_must_join_final_function_engine_counts(self) -> None:
        self.health['function_accounting'].update(started='3', ended='3')
        with self.assertRaises(ValueError): self.verify()

    def test_delta_or_nonmonotonic_metric_contract_cannot_be_accepted(self) -> None:
        calls = self.metric_scope.metrics[0].sum
        for temporality, monotonic in ((1, True), (0, True), (2, False)):
            calls.aggregation_temporality, calls.is_monotonic = temporality, monotonic
            with self.subTest(temporality=temporality, monotonic=monotonic), self.assertRaises(ValueError): self.verify()
        calls.aggregation_temporality, calls.is_monotonic = 2, True
        self.metric_scope.metrics[1].histogram.aggregation_temporality = 1
        with self.assertRaises(ValueError): self.verify()

    def test_reordered_actual_prefixes_are_sorted_without_losing_the_cumulative_state(self) -> None:
        calls = self.metric_scope.metrics[0].sum
        point = calls.data_points.add(); point.CopyFrom(calls.data_points[0])
        point.as_int, point.time_unix_nano = 1, 1
        duration = self.metric_scope.metrics[1].histogram
        point = duration.data_points.add(); point.CopyFrom(duration.data_points[0])
        point.count, point.bucket_counts[0], point.time_unix_nano = 1, 1, 1
        self.health['metric_exports'].update(units_started='4', units_succeeded='4')
        self.verify()

    def test_changed_cumulative_start_count_sum_and_missing_timestamps_refuse(self) -> None:
        original = self.capture.metrics[0].SerializeToString()
        for change in ('start', 'counter', 'histogram', 'timestamp', 'flags', 'same-time'):
            self.capture.metrics[0].ParseFromString(original)
            metrics = self.capture.metrics[0].resource_metrics[0].scope_metrics[0].metrics
            if change in ('start', 'counter', 'flags'):
                points = metrics[0].sum.data_points
                point = points.add(); point.CopyFrom(points[0]); point.time_unix_nano = 3
                if change == 'start': point.start_time_unix_nano = 2
                elif change == 'counter': point.as_int = 1
                else: point.flags = 1
            elif change == 'histogram':
                points = metrics[1].histogram.data_points
                point = points.add(); point.CopyFrom(points[0]); point.time_unix_nano = 3
                point.max, point.sum = 1e-7, 1e-7
            elif change == 'same-time':
                points = metrics[1].histogram.data_points
                point = points.add(); point.CopyFrom(points[0])
                point.count, point.bucket_counts[0] = 1, 1
            else:
                metrics[0].sum.data_points[0].start_time_unix_nano = 0
            self.health['metric_exports'].update(units_started='3', units_succeeded='3')
            with self.subTest(change=change), self.assertRaises(ValueError): self.verify()

    def test_gauge_observation_time_does_not_require_a_cumulative_start(self) -> None:
        message = ExportMetricsServiceRequest()
        point = message.resource_metrics.add().scope_metrics.add().metrics.add().gauge.data_points.add(
            as_int=0, time_unix_nano=2)
        bounds = self.policy.data['metrics']['histogram_boundaries_seconds']
        receipt.cumulative([('otelc.lifetime.live', 'gauge', point)], bounds)
        point.time_unix_nano = 0
        with self.assertRaisesRegex(ValueError, 'gauge observation time'):
            receipt.cumulative([('otelc.lifetime.live', 'gauge', point)], bounds)


if __name__ == '__main__':
    unittest.main()
