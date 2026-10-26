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
        point = calls.sum.data_points.add(as_int=2)
        point.attributes.add(key='function').value.string_value = 'app.rs::workload'
        duration = scope.metrics.add(name='otelc.function.duration')
        bounds = self.policy.data['metrics']['histogram_boundaries_seconds']
        point = duration.histogram.data_points.add(count=2, bucket_counts=[2]+[0]*len(bounds), explicit_bounds=bounds)
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
                point = metric.histogram.data_points.add(count=1, bucket_counts=[1]+[0]*len(bounds), explicit_bounds=bounds)
            else:
                point = getattr(metric, kind).data_points.add(as_int=0 if name == 'live' else 1)
            point.attributes.add(key='boundary').value.string_value = 'drop_entry'
            point.attributes.add(key='type').value.string_value = 'Item'
        span = self.span_scope.spans.add(name='Item lifetime', trace_id=b'\1'*16, span_id=b'\3'*8)
        span.links.add(trace_id=b'\1'*16, span_id=b'\1'*8)

        def verify(lifetimes=None):
            return receipt.verify(self.capture, policy, self.health, function='app.rs::workload',
                                  expected_spans=2, expected_calls=2,
                                  lifetimes={'Item lifetime': 1} if lifetimes is None else lifetimes)

        self.assertEqual(verify()['lifetime_spans'], {'Item lifetime': 1})
        with self.assertRaises(ValueError): verify({'wrong lifetime': 1})
        span.links[0].span_id = b'\4'*8
        with self.assertRaisesRegex(ValueError, 'original function link'): verify()

    def test_lifetime_metrics_cannot_drop_types_values_bounds_or_instruments(self) -> None:
        bounds = self.policy.data['metrics']['histogram_boundaries_seconds']
        metrics = []
        for name, kind in (('admitted', 'sum'), ('completed', 'sum'), ('duration', 'histogram'), ('live', 'gauge')):
            message = ExportMetricsServiceRequest()
            metric = message.resource_metrics.add().scope_metrics.add().metrics.add(name='otelc.lifetime.'+name)
            point = (metric.histogram.data_points.add(count=1, bucket_counts=[1]+[0]*len(bounds), explicit_bounds=bounds)
                     if kind == 'histogram' else getattr(metric, kind).data_points.add(as_int=0 if name == 'live' else 1))
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


if __name__ == '__main__':
    unittest.main()
