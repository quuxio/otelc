"""Qualify independent local trees which share a distributed trace identity."""
import time
import unittest

from opentelemetry.sdk.trace import IdGenerator

from test_python_spans import PythonSpanHarness, trace_plan


class SharedTraceIds(IdGenerator):
    def __init__(self, span=0):
        self.span = span

    def generate_trace_id(self):
        return 0x1234567890ABCDEF1234567890ABCDEF

    def generate_span_id(self):
        self.span += 1
        return self.span


class PythonTraceOwnershipTests(PythonSpanHarness, unittest.TestCase):
    def store(self, policy=None, span=0):
        runtime, _, capture = self.runtime(policy)
        runtime.traces.tracer.id_generator = SharedTraceIds(span)
        return runtime, runtime.traces, capture

    def test_shared_trace_id_roots_complete_and_export_independently(self):
        runtime, store, capture = self.store()
        with runtime.lock:
            first = store.begin(None, 'first', time.monotonic_ns())
            second = store.begin(None, 'second', time.monotonic_ns())
            self.assertTrue(second.sampled)
            child = store.begin(first, 'child', time.monotonic_ns())
            store.finish(first, time.monotonic_ns())
            store.finish(child, time.monotonic_ns())
            self.assertEqual(store.report()['active_trees'], 1)
            self.assertEqual(store.report()['completed_trees'], 1)
            store.finish(second, time.monotonic_ns())
        report = runtime.close()
        spans = capture.spans()
        self.assertEqual([span.name for span in spans], ['first', 'child', 'second'])
        self.assertEqual(len({span.trace_id for span in spans}), 1)
        self.assertFalse(spans[0].parent_span_id)
        self.assertFalse(spans[2].parent_span_id)
        self.assertEqual(spans[1].parent_span_id, spans[0].span_id)
        self.assertEqual(report['traces']['completed_trees'], 2)
        self.assertFalse(report['traces']['losses'])
        self.assertEqual(store.retained, 0)

    def test_capacity_rejection_invalidates_only_its_local_tree(self):
        policy = trace_plan()
        policy['traces']['max_spans_per_trace'] = 1
        runtime, store, capture = self.store(policy)
        with runtime.lock:
            first = store.begin(None, 'first', time.monotonic_ns())
            second = store.begin(None, 'second', time.monotonic_ns())
            self.assertTrue(second.sampled)
            rejected = store.begin(first, 'overflow', time.monotonic_ns())
            self.assertFalse(rejected.sampled)
            store.finish(first, time.monotonic_ns())
            store.finish(second, time.monotonic_ns())
        report = runtime.close()
        self.assertEqual([span.name for span in capture.spans()], ['second'])
        self.assertEqual(report['traces']['losses'], {'span_capacity': 1})
        self.assertEqual(store.retained, 0)

    def test_late_child_and_lease_do_not_delay_the_other_local_root(self):
        runtime, store, capture = self.store()
        with runtime.lock:
            first = store.begin(None, 'first', time.monotonic_ns())
            lease = store.acquire(first)
            self.assertIsNotNone(lease)
            store.finish(first, time.monotonic_ns())
            second = store.begin(None, 'second', time.monotonic_ns())
            self.assertTrue(second.sampled)
            store.finish(second, time.monotonic_ns())
            self.assertEqual(store.report()['completed_trees'], 1)
            late = store.begin(first, 'late', time.monotonic_ns())
            self.assertTrue(late.sampled)
            store.finish(late, time.monotonic_ns())
            store.release(lease)
            store.release(lease)
            self.assertEqual(store.report()['completed_trees'], 2)
            self.assertIsNone(store.acquire(second))
        report = runtime.close()
        spans = capture.spans()
        self.assertEqual([span.name for span in spans], ['second', 'first', 'late'])
        self.assertEqual(spans[2].parent_span_id, spans[1].span_id)
        self.assertFalse(report['traces']['pending_contexts'])
        self.assertFalse(report['traces']['losses'])

    def test_expired_context_cannot_attach_to_a_new_tree_with_the_same_trace_id(self):
        runtime, store, capture = self.store()
        with runtime.lock:
            expired = store.begin(None, 'old', time.monotonic_ns())
            store.finish(expired, time.monotonic_ns())
            current = store.begin(None, 'current', time.monotonic_ns())
            late = store.begin(expired, 'stale', time.monotonic_ns())
            self.assertFalse(late.sampled)
            self.assertIsNone(store.acquire(expired))
            store.reject(expired, 'stale_rejection')
            store.finish(expired, time.monotonic_ns())
            store.finish(current, time.monotonic_ns())
        report = runtime.close()
        self.assertEqual([span.name for span in capture.spans()], ['old', 'current'])
        self.assertEqual(report['traces']['losses'], {'context_expired': 1})
        self.assertEqual(report['traces']['completed_trees'], 2)

    def test_context_from_another_store_cannot_invalidate_or_join_this_store(self):
        first_runtime, first_store, first_capture = self.store()
        second_runtime, second_store, second_capture = self.store(span=100)
        with first_runtime.lock, second_runtime.lock:
            first = first_store.begin(None, 'first', time.monotonic_ns())
            second = second_store.begin(None, 'second', time.monotonic_ns())
            self.assertIsNone(second_store.acquire(first))
            self.assertFalse(second_store.begin(first, 'foreign', time.monotonic_ns()).sampled)
            second_store.reject(first, 'foreign_rejection')
            second_store.finish(first, time.monotonic_ns())
            second_store.finish(second, time.monotonic_ns())
            first_store.finish(first, time.monotonic_ns())
        first_runtime.close()
        report = second_runtime.close()
        self.assertEqual([span.name for span in first_capture.spans()], ['first'])
        self.assertEqual([span.name for span in second_capture.spans()], ['second'])
        self.assertEqual(report['traces']['losses'], {'context_expired': 1})

    def test_shared_trace_ids_do_not_bypass_active_local_tree_limit(self):
        policy = trace_plan()
        policy['traces']['max_active_traces'] = 1
        runtime, store, capture = self.store(policy)
        with runtime.lock:
            first = store.begin(None, 'first', time.monotonic_ns())
            rejected = store.begin(None, 'overlap', time.monotonic_ns())
            self.assertFalse(rejected.sampled)
            store.finish(first, time.monotonic_ns())
            second = store.begin(None, 'second', time.monotonic_ns())
            self.assertTrue(second.sampled)
            self.assertIsNone(store.acquire(first))
            store.finish(second, time.monotonic_ns())
        report = runtime.close()
        self.assertEqual([span.name for span in capture.spans()], ['first', 'second'])
        self.assertEqual(report['traces']['losses'], {'trace_capacity': 1})
