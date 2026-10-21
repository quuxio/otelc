"""Verify link state on actual OTLP bytes and keep application SDK encoding intact."""
import time
import unittest
from unittest.mock import patch

from test_python_spans import Capture, Runtime, receiver, trace_plan
from quux_otelc_python.carrier import extract
from quux_otelc_python.traces import remote_context
from quux_otelc_python import trace_wire
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans as sdk_encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Link


PARENT = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-03'


def linked_span(name='client', service='one'):
    provider = TracerProvider(resource=Resource.create({'service.name': service}), shutdown_on_exit=False)
    context = remote_context(extract([PARENT], ['a=1,b=2']))
    span = provider.get_tracer('wire-test').start_span(name, links=[Link(context, {'source': 'application-header'})])
    span.end()
    provider.shutdown()
    return span


class PythonTraceWireTests(unittest.TestCase):
    def test_correction_changes_only_missing_link_state_and_supports_sdk_resource_grouping(self):
        spans = [linked_span('first', 'one'), linked_span('second', 'two'), linked_span('third', 'one')]
        original = sdk_encode_spans(spans)
        expected = ExportTraceServiceRequest()
        expected.CopyFrom(original)
        for resource in expected.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    self.assertFalse(span.links[0].trace_state)
                    span.links[0].trace_state = 'a=1,b=2'
        corrected = trace_wire.encode_spans(spans)
        self.assertEqual(corrected, expected)
        self.assertEqual(sdk_encode_spans(spans), original)

    def test_default_profile_keeps_links_disabled_and_preserves_resource_and_span(self):
        with receiver() as (endpoint, requests):
            policy = trace_plan()
            policy['trace_export']['endpoint'] = endpoint + '/v1/traces'
            runtime = Runtime(policy, Capture())
            self.addCleanup(runtime.close)
            with runtime.lock:
                context = remote_context(extract([PARENT], ['a=1,b=2']))
                root = runtime.traces.begin(None, 'client', time.perf_counter_ns(), links=[Link(context)])
                runtime.traces.finish(root, time.perf_counter_ns())
            report = runtime.close()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][0], '/v1/traces')
            decoded = ExportTraceServiceRequest.FromString(requests[0][2])
            resource = decoded.resource_spans[0]
            span = resource.scope_spans[0].spans[0]
            self.assertEqual(span.name, 'client')
            # Links are disabled unless HTTP or lifetime recording requests them.
            self.assertFalse(span.links)
            self.assertFalse(report['export_loss'])
            self.assertTrue(resource.resource.attributes)

    def test_real_http_profile_exports_link_state_and_wire_limit_failure_recovers(self):
        with receiver() as (endpoint, requests):
            policy = trace_plan()
            policy['propagation'] = {'http': True, 'tasks': False}
            policy['trace_export']['endpoint'] = endpoint + '/v1/traces'
            runtime = Runtime(policy, Capture())
            self.addCleanup(runtime.close)
            context = remote_context(extract([PARENT], ['a=1,b=2']))
            with runtime.lock:
                first = runtime.traces.begin(None, 'too large', time.perf_counter_ns(), links=[Link(context)])
                runtime.traces.finish(first, time.perf_counter_ns())
            with patch.object(trace_wire, 'MAX_BYTES', 1):
                runtime.traces.flush()
            self.assertFalse(requests)
            with runtime.lock:
                second = runtime.traces.begin(None, 'valid', time.perf_counter_ns(), links=[Link(context)])
                runtime.traces.finish(second, time.perf_counter_ns())
            report = runtime.close()
            self.assertEqual(len(requests), 1)
            decoded = ExportTraceServiceRequest.FromString(requests[0][2])
            span = decoded.resource_spans[0].scope_spans[0].spans[0]
            self.assertEqual(span.name, 'valid')
            self.assertEqual(span.links[0].trace_state, 'a=1,b=2')
            self.assertEqual(span.links[0].trace_id.hex(), PARENT.split('-')[1])
            self.assertEqual(span.links[0].span_id.hex(), PARENT.split('-')[2])
            self.assertEqual(report['export_loss'], 1)

    def test_mismatched_or_missing_sdk_links_and_duplicate_spans_fail_closed(self):
        span = linked_span()
        with self.assertRaisesRegex(ValueError, 'duplicate SDK'):
            trace_wire.encode_spans([span, span])
        for change in ('link', 'missing', 'span'):
            encoded = sdk_encode_spans([span])
            target = encoded.resource_spans[0].scope_spans[0].spans[0]
            if change == 'link':
                target.links[0].span_id = bytes(8)
            elif change == 'missing':
                del target.links[:]
            else:
                del encoded.resource_spans[:]
            with self.subTest(change=change), self.assertRaises((ValueError, KeyError)):
                trace_wire.restore_link_states(encoded, [span])
