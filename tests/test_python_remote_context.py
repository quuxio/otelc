"""Decode real SDK output for independent local trees with remote wire parents."""
import json
import time
import unittest
from pathlib import Path

from test_python_spans import PythonSpanHarness, trace_plan
from quux_otelc_python.carrier import extract, from_sdk, inject
from opentelemetry.trace import Link, SpanKind, TraceFlags


PARENT = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-03'


class PythonRemoteContextTests(PythonSpanHarness, unittest.TestCase):
    def test_remote_roots_keep_trace_parent_state_flags_and_independent_ownership(self):
        runtime, _, capture = self.runtime()
        store = runtime.traces
        remote = extract([PARENT], ['first=1,second=2'])
        other = extract([PARENT.replace('00f067aa0ba902b7', '00f067aa0ba902b8')], ['other=3'])
        with runtime.lock:
            first = store.begin(None, 'server one', time.perf_counter_ns(), remote=remote, kind=SpanKind.SERVER)
            second = store.begin(None, 'server two', time.perf_counter_ns(), remote=other, kind=SpanKind.SERVER)
            child = store.begin(first, 'client', time.perf_counter_ns(), kind=SpanKind.CLIENT)
            self.assertIsNot(first.local_root, second.local_root)
            self.assertEqual(first.context.trace_flags, TraceFlags(3))
            store.finish(first, time.perf_counter_ns())
            store.finish(child, time.perf_counter_ns())
            self.assertEqual(store.report()['active_trees'], 1)
            store.finish(second, time.perf_counter_ns())
        report = runtime.close()
        spans = capture.spans()
        self.assertEqual([span.name for span in spans], ['server one', 'client', 'server two'])
        self.assertEqual(len({span.trace_id for span in spans}), 1)
        self.assertEqual(spans[0].parent_span_id.hex(), '00f067aa0ba902b7')
        self.assertEqual(spans[2].parent_span_id.hex(), '00f067aa0ba902b8')
        self.assertEqual(spans[1].parent_span_id, spans[0].span_id)
        self.assertEqual([span.kind for span in spans], [2, 3, 2])
        self.assertEqual([span.trace_state for span in spans], ['first=1,second=2', 'first=1,second=2', 'other=3'])
        self.assertFalse(report['traces']['losses'])

    def test_remote_unsampled_context_propagates_without_local_spans_or_leases(self):
        runtime, _, capture = self.runtime()
        store = runtime.traces
        remote = extract([PARENT[:-2] + '02'], ['vendor=unsampled'])
        with runtime.lock:
            server = store.begin(None, 'server', time.perf_counter_ns(), remote=remote, kind=SpanKind.SERVER)
            child = store.begin(server, 'child', time.perf_counter_ns())
            self.assertFalse(server.sampled)
            self.assertIsNone(store.acquire(server))
            outgoing = inject(from_sdk(child.context))
            self.assertEqual(outgoing['traceparent'].split('-')[1], PARENT.split('-')[1])
            self.assertTrue(outgoing['traceparent'].endswith('-02'))
            self.assertEqual(outgoing['tracestate'], 'vendor=unsampled')
            store.finish(server, time.perf_counter_ns())
            store.finish(child, time.perf_counter_ns())
        report = runtime.close()
        self.assertFalse(capture.spans())
        self.assertEqual(report['traces']['sampled_out_roots'], 1)
        self.assertFalse(report['traces']['active_trees'])
        self.assertFalse(report['traces']['pending_contexts'])
        self.assertFalse(report['traces']['losses'])

    def test_sampled_remote_parent_overrides_local_root_sampling_zero(self):
        policy = trace_plan()
        policy['traces']['root_sample_ratio'] = 0
        runtime, _, capture = self.runtime(policy)
        with runtime.lock:
            root = runtime.traces.begin(None, 'remote', time.perf_counter_ns(), remote=extract([PARENT]))
            self.assertTrue(root.sampled)
            runtime.traces.finish(root, time.perf_counter_ns())
        runtime.close()
        self.assertEqual(len(capture.spans()), 1)

    def test_every_valid_wire_vector_survives_python_sdk_conversion_without_payload_logs(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/trace-context.json').read_text())['cases']
        runtime, _, _ = self.runtime()
        for case in cases:
            remote = extract(case['traceparent'], case['tracestate'])
            if remote is None:
                continue
            with self.subTest(name=case['name']), self.assertNoLogs('opentelemetry.trace.span', level='WARNING'):
                with runtime.lock:
                    root = runtime.traces.begin(None, 'server', time.perf_counter_ns(), remote=remote)
                    forwarded = inject(from_sdk(root.context))
                    self.assertEqual(forwarded.get('tracestate'), case['expected']['tracestate'])
                    runtime.traces.finish(root, time.perf_counter_ns())
                runtime.traces.flush()

    def test_local_and_remote_parents_cannot_be_combined_and_links_are_explicit(self):
        policy = trace_plan()
        policy['propagation'] = {'http': True, 'tasks': False}
        runtime, _, capture = self.runtime(policy)
        store = runtime.traces
        with runtime.lock:
            local = store.begin(None, 'local', time.perf_counter_ns())
            with self.assertRaisesRegex(ValueError, 'local and remote'):
                store.begin(local, 'ambiguous', time.perf_counter_ns(), remote=extract([PARENT]))
            client = store.begin(local, 'client', time.perf_counter_ns(), kind=SpanKind.CLIENT,
                                 links=[Link(local.context)])
            store.finish(client, time.perf_counter_ns())
            store.finish(local, time.perf_counter_ns())
        runtime.close()
        self.assertEqual(len(capture.spans()[1].links), 1)
        self.assertEqual(capture.spans()[1].links[0].span_id, capture.spans()[0].span_id)
