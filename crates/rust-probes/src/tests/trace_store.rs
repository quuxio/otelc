use super::*;
use std::time::Duration;
fn policy() -> Traces {
    Traces {
        enabled: true,
        root_sample_ratio: 1.0,
        max_active_traces: 2,
        max_spans_per_trace: 4,
    }
}
fn begin(store: &mut Store, parent: Option<Context>, name: &str, policy: &Traces) -> Context {
    store.enter(1, parent, Arc::from(name), Instant::now(), policy)
}
fn finish(store: &mut Store, context: Context) {
    store.finish(context, Instant::now(), false, false, 2);
}
#[test]
fn complete_tree_ids_parents_and_monotonic_times() {
    let mut store = Store::default();
    let policy = policy();
    let root = begin(&mut store, None, "root", &policy);
    let first = begin(&mut store, Some(root), "first", &policy);
    let leaf = begin(&mut store, Some(first), "leaf", &policy);
    finish(&mut store, leaf);
    finish(&mut store, first);
    assert!(store.pop().is_none());
    finish(&mut store, root);
    let spans = store.pop().unwrap();
    assert_eq!(spans.len(), 3);
    assert_eq!(spans[0].parent_span_id, SpanId::INVALID);
    assert_eq!(spans[1].parent_span_id, spans[0].span_context.span_id());
    assert_eq!(spans[2].parent_span_id, spans[1].span_context.span_id());
    for span in &spans {
        assert!(span.span_context.is_valid());
        assert!(span.span_context.is_sampled());
        assert_eq!(
            span.span_context.trace_id(),
            spans[0].span_context.trace_id()
        );
        assert!(span.end_time >= span.start_time);
        assert!(span.start_time >= spans[0].start_time);
        assert!(span.end_time <= spans[0].end_time);
    }
    assert_eq!(store.retained, 0);
    assert_eq!(store.completed, 1);
    assert!(store.losses.is_empty());
}
#[test]
fn parent_closed_before_child_waits_for_whole_tree() {
    let mut store = Store::default();
    let policy = policy();
    let root = begin(&mut store, None, "root", &policy);
    let child = begin(&mut store, Some(root), "child", &policy);
    finish(&mut store, root);
    assert!(store.pop().is_none());
    finish(&mut store, child);
    assert_eq!(store.pop().unwrap().len(), 2);
    let late = begin(&mut store, Some(root), "late", &policy);
    assert!(!late.sampled);
    finish(&mut store, late);
    assert!(store.roots.is_empty());
}
#[test]
fn sampling_is_inherited_and_unsampled_is_not_loss() {
    let mut store = Store::default();
    let mut policy = policy();
    policy.root_sample_ratio = 0.0;
    let root = begin(&mut store, None, "root", &policy);
    policy.root_sample_ratio = 1.0;
    let child = begin(&mut store, Some(root), "child", &policy);
    assert!(!child.sampled);
    finish(&mut store, child);
    finish(&mut store, root);
    assert_eq!(store.sampled_out, 1);
    assert!(store.pop().is_none());
    assert!(store.losses.is_empty());
}
#[test]
fn span_capacity_discards_whole_tree_including_finished_siblings() {
    let mut store = Store::default();
    let mut policy = policy();
    policy.max_spans_per_trace = 2;
    let root = begin(&mut store, None, "root", &policy);
    let child = begin(&mut store, Some(root), "child", &policy);
    finish(&mut store, child);
    let rejected = begin(&mut store, Some(root), "third", &policy);
    assert!(!rejected.sampled);
    assert_eq!(store.roots[&root.root].nodes.capacity(), 0);
    assert_eq!(store.roots[&root.root].positions.capacity(), 0);
    let descendant = begin(&mut store, Some(rejected), "descendant", &policy);
    assert!(!descendant.sampled);
    finish(&mut store, root);
    assert!(store.pop().is_none());
    assert_eq!(store.losses["span_capacity"], 1);
    assert_eq!(store.retained, 0);
}
#[test]
fn active_root_queue_and_total_memory_capacity_are_explicit() {
    let mut store = Store::default();
    let mut policy = policy();
    policy.max_active_traces = 1;
    let root = begin(&mut store, None, "root", &policy);
    let rejected = begin(&mut store, None, "rejected", &policy);
    assert!(!rejected.sampled);
    finish(&mut store, root);
    let second = begin(&mut store, None, "second", &policy);
    store.finish(second, Instant::now(), false, false, 1);
    assert_eq!(store.losses["trace_capacity"], 1);
    assert_eq!(store.losses["queue_capacity"], 1);
    assert_eq!(store.pop().unwrap().len(), 1);
    assert_eq!(store.retained, 0);
    store.retained = 1_048_576;
    assert!(!begin(&mut store, None, "budget", &policy).sampled);
    store.retained = 0;
    let root = begin(&mut store, None, "root", &policy);
    store.retained = 1_048_576;
    assert!(!begin(&mut store, Some(root), "child", &policy).sampled);
    finish(&mut store, root);
    store.retained = 0;
    store.next = u64::MAX;
    assert!(!begin(&mut store, None, "overflow", &policy).sampled);
    assert_eq!(store.losses["invalid"], 1);
}
#[test]
fn rejection_invalidates_once_and_shutdown_discards_incomplete_trees() {
    let mut store = Store::default();
    let policy = policy();
    let root = begin(&mut store, None, "root", &policy);
    let child = begin(&mut store, Some(root), "child", &policy);
    let rejected = store.reject(1, Some(child), "function_capacity");
    store.reject(1, Some(rejected), "function_capacity");
    finish(&mut store, root);
    finish(&mut store, child);
    assert!(store.pop().is_none());
    assert_eq!(store.losses["function_capacity"], 1);
    let _pending = begin(&mut store, None, "pending", &policy);
    store.shutdown();
    assert_eq!(store.losses["incomplete"], 1);
    assert_eq!(store.retained, 0);
    assert!(store.roots.is_empty());
    finish(&mut store, root);
}
#[test]
fn cancellation_and_escaping_unwind_status_do_not_capture_payloads() {
    let mut store = Store::default();
    let policy = policy();
    let root = begin(&mut store, None, "cancel", &policy);
    store.finish(root, Instant::now(), false, true, 2);
    let panic = begin(&mut store, None, "panic", &policy);
    store.finish(panic, Instant::now(), true, false, 2);
    let cancel = store.pop().unwrap();
    assert_eq!(cancel[0].status, Status::error("cancelled"));
    assert_eq!(cancel[0].attributes.len(), 2);
    assert_eq!(
        store.pop().unwrap()[0].status,
        Status::error("escaping unwind")
    );
}
#[test]
fn timestamps_use_one_epoch_and_clamp_earlier_finish() {
    let mut store = Store::default();
    let policy = policy();
    let root = begin(&mut store, None, "root", &policy);
    let tree = store.roots.get_mut(&root.root).unwrap();
    tree.epoch = SystemTime::UNIX_EPOCH + Duration::from_secs(10);
    let origin = tree.origin;
    store.finish(root, origin - Duration::from_nanos(1), false, false, 1);
    let spans = store.pop().unwrap();
    assert_eq!(
        spans[0].start_time,
        SystemTime::UNIX_EPOCH + Duration::from_secs(10)
    );
    assert_eq!(spans[0].end_time, spans[0].start_time);
}
#[test]
fn thread_scopes_restore_on_unwind_and_do_not_leak_to_other_threads() {
    let mut store = Store::default();
    let root = begin(&mut store, None, "root", &policy());
    assert!(current(1).is_none());
    let _outer = Scope::attach(Some(root));
    assert!(current(1).is_some());
    assert!(current(2).is_none());
    std::thread::spawn(|| assert!(current(1).is_none()))
        .join()
        .unwrap();
    let payload = std::panic::catch_unwind(|| {
        let _nested = Scope::attach(None);
        assert!(current(1).is_none());
        panic!("original");
    })
    .unwrap_err();
    assert_eq!(*payload.downcast::<&str>().unwrap(), "original");
    assert!(current(1).is_some());
}
#[test]
fn oversized_payload_discards_the_whole_tree_before_name_duplication() {
    let mut store = Store::default();
    let mut policy = policy();
    policy.max_spans_per_trace = 8192;
    let name = "x".repeat(1024);
    let root = begin(&mut store, None, &name, &policy);
    for _ in 1..8192 {
        let child = begin(&mut store, Some(root), &name, &policy);
        finish(&mut store, child);
    }
    finish(&mut store, root);
    assert!(store.pop().unwrap().is_empty());
    assert_eq!(store.losses["batch_bytes"], 1);
    assert_eq!(store.retained, 0);
}

#[test]
fn zero_ids_are_retried_boundedly_and_never_exported() {
    let mut calls = 0;
    assert_eq!(
        valid_id(
            || {
                calls += 1;
                if calls == 3 {
                    7
                } else {
                    0
                }
            },
            0
        ),
        Some(7)
    );
    assert_eq!(calls, 3);
    calls = 0;
    assert_eq!(
        valid_id(
            || {
                calls += 1;
                0
            },
            0
        ),
        None
    );
    assert_eq!(calls, 3);
}
