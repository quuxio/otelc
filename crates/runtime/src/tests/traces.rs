use super::*;

fn policy() -> Traces {
    Traces {
        enabled: true,
        root_sample_ratio: 1.0,
        max_active_traces: 2,
        max_spans_per_trace: 8,
    }
}
fn bridge<'a>(store: &'a Mutex<Store>, policy: &'a Traces) -> Bridge<'a> {
    Bridge::new(
        store,
        policy,
        [Arc::from("root"), Arc::from("child")],
        (2, 2),
        (
            100,
            Instant::now() - Duration::from_secs(60),
            SystemTime::UNIX_EPOCH + Duration::from_secs(1000),
        ),
    )
}
fn entry(slot: u32, token: u64) -> Record {
    Record {
        invocation: token,
        trace_root: token,
        start_tick: 100,
        thread_slot: slot,
        flags: ROOT_ENTER,
        ..Default::default()
    }
}
fn exit(slot: u32, token: u64, root: u64, parent: u64, count: u32) -> Record {
    Record {
        invocation: token,
        trace_root: root,
        parent_invocation: parent,
        start_tick: if parent == 0 { 100 } else { 102 },
        duration_tick: if parent == 0 { 10 } else { 2 },
        function_key: u64::from(parent != 0),
        thread_slot: slot,
        flags: TRACE_EXIT,
        reserved: count,
        ..Default::default()
    }
}
fn loss(store: &Mutex<Store>, reason: &str) -> u64 {
    store
        .lock()
        .unwrap()
        .losses
        .get(reason)
        .copied()
        .unwrap_or_default()
}
#[test]
fn complete_postorder_records_produce_sdk_parents_and_producer_times() {
    let store = Mutex::new(Store::default());
    let policy = policy();
    let mut bridge = bridge(&store, &policy);
    bridge.record(entry(0, 1));
    bridge.record(exit(0, 2, 1, 1, 0));
    assert!(store.lock().unwrap().pop().is_none());
    bridge.record(exit(0, 1, 1, 0, 2));
    let spans = store.lock().unwrap().pop().unwrap();
    assert_eq!(spans.len(), 2);
    assert_eq!(spans[1].parent_span_id, spans[0].span_context.span_id());
    assert_eq!(
        spans[1].span_context.trace_id(),
        spans[0].span_context.trace_id()
    );
    assert_eq!(
        spans[0].start_time,
        SystemTime::UNIX_EPOCH + Duration::from_secs(1000)
    );
    assert_eq!(
        spans[0]
            .end_time
            .duration_since(spans[0].start_time)
            .unwrap(),
        Duration::from_nanos(10)
    );
    assert_eq!(
        spans[1]
            .start_time
            .duration_since(spans[0].start_time)
            .unwrap(),
        Duration::from_nanos(2)
    );
    assert_eq!(bridge.retained, 0);
}
#[test]
fn root_wall_epoch_is_refreshed_after_idle_or_clock_adjustment() {
    let store = Mutex::new(Store::default());
    let policy = policy();
    let mut bridge = bridge(&store, &policy);
    let mut root = entry(0, 1);
    root.duration_tick = 2_000_000_000_000;
    bridge.record(root);
    bridge.record(exit(0, 1, 1, 0, 1));
    let spans = store.lock().unwrap().pop().unwrap();
    assert_eq!(
        spans[0].start_time,
        SystemTime::UNIX_EPOCH + Duration::from_secs(2000)
    );
    assert_eq!(
        spans[0]
            .end_time
            .duration_since(spans[0].start_time)
            .unwrap(),
        Duration::from_nanos(10)
    );
}
#[test]
fn producer_rejected_root_suppresses_children_and_reuses_worker_slot() {
    let store = Mutex::new(Store::default());
    let policy = policy();
    let mut bridge = bridge(&store, &policy);
    let mut root = entry(0, 1);
    root.flags |= ROOT_DENIED;
    bridge.record(root);
    bridge.record(exit(0, 2, 1, 1, 0));
    bridge.record(exit(0, 1, 1, 0, 2));
    assert!(store.lock().unwrap().pop().is_none());
    assert_eq!(loss(&store, "trace_capacity"), 1);
    bridge.record(entry(0, 3));
    bridge.record(exit(0, 3, 3, 0, 1));
    assert_eq!(store.lock().unwrap().pop().unwrap().len(), 1);
    assert_eq!(bridge.retained, 0);
}
#[test]
fn lost_child_or_root_records_never_export_partial_trees() {
    for missing in ["entry", "child", "exit"] {
        let store = Mutex::new(Store::default());
        let policy = policy();
        let mut bridge = bridge(&store, &policy);
        if missing != "entry" {
            bridge.record(entry(0, 1));
        }
        if missing != "child" {
            bridge.record(exit(0, 2, 1, 1, 0));
        }
        if missing != "exit" {
            bridge.record(exit(0, 1, 1, 0, 2));
        }
        bridge.shutdown();
        let mut store = store.lock().unwrap();
        assert!(store.pop().is_none(), "{missing}");
        assert_eq!(store.losses.values().sum::<u64>(), 1, "{missing}");
        assert_eq!(store.report()["active_trees"], 0);
        assert_eq!(bridge.retained, 0);
    }
}
#[test]
fn producer_loss_and_span_overflow_discard_already_recorded_children() {
    for reason in ["producer_loss", "span_capacity"] {
        let store = Mutex::new(Store::default());
        let mut policy = policy();
        if reason == "span_capacity" {
            policy.max_spans_per_trace = 1;
        }
        let mut bridge = bridge(&store, &policy);
        bridge.record(entry(0, 1));
        bridge.record(exit(0, 2, 1, 1, 0));
        let mut root = exit(0, 1, 1, 0, 2);
        if reason == "producer_loss" {
            root.flags |= INVALID_ROOT;
        }
        bridge.record(root);
        assert!(store.lock().unwrap().pop().is_none());
        assert_eq!(loss(&store, reason), 1);
        assert_eq!(store.lock().unwrap().report()["active_trees"], 0);
        assert_eq!(bridge.retained, 0);
    }
}
#[test]
fn retirement_reuse_and_replaced_root_release_incomplete_tree() {
    for retire in [true, false] {
        let store = Mutex::new(Store::default());
        let policy = policy();
        let mut bridge = bridge(&store, &policy);
        bridge.record(entry(0, 1));
        bridge.record(exit(0, 2, 1, 1, 0));
        if retire {
            bridge.retire(0);
        }
        bridge.record(entry(0, 3));
        bridge.record(exit(0, 3, 3, 0, 1));
        assert_eq!(store.lock().unwrap().pop().unwrap().len(), 1);
        assert!(store.lock().unwrap().pop().is_none());
        assert_eq!(loss(&store, "incomplete"), 1);
        assert_eq!(bridge.retained, 0);
    }
}
#[test]
fn independent_slots_and_root_sampling_do_not_promote_children() {
    for ratio in [0.0, 1.0] {
        let store = Mutex::new(Store::default());
        let mut policy = policy();
        policy.root_sample_ratio = ratio;
        let mut bridge = bridge(&store, &policy);
        bridge.record(entry(0, 1));
        bridge.record(entry(1, 1));
        bridge.record(exit(1, 2, 1, 1, 0));
        bridge.record(exit(0, 1, 1, 0, 1));
        bridge.record(exit(1, 1, 1, 0, 2));
        let mut store = store.lock().unwrap();
        if ratio == 1.0 {
            let first = store.pop().unwrap();
            let second = store.pop().unwrap();
            assert_eq!((first.len(), second.len()), (1, 2));
            assert_ne!(
                first[0].span_context.trace_id(),
                second[0].span_context.trace_id()
            );
        } else {
            assert!(store.pop().is_none());
            assert_eq!(store.sampled_out, 2);
        }
        assert!(store.losses.is_empty());
        assert_eq!(bridge.retained, 0);
    }
}
#[test]
fn malformed_relationships_tokens_keys_and_intervals_invalidate_entire_tree() {
    for variant in 0..7 {
        let store = Mutex::new(Store::default());
        let policy = policy();
        let mut bridge = bridge(&store, &policy);
        bridge.record(entry(0, 1));
        let mut child = exit(0, 2, 1, 1, 0);
        match variant {
            0 => child.parent_invocation = 99,
            1 => child.parent_invocation = 2,
            2 => child.function_key = 99,
            3 => child.start_tick = 99,
            4 => child.duration_tick = 99,
            5 => child.duration_tick = u64::MAX,
            _ => child.invocation = 0,
        }
        bridge.record(child);
        bridge.record(exit(0, 1, 1, 0, 2));
        assert!(store.lock().unwrap().pop().is_none(), "{variant}");
        assert_eq!(
            store.lock().unwrap().losses.values().sum::<u64>(),
            1,
            "{variant}"
        );
        assert_eq!(store.lock().unwrap().report()["active_trees"], 0);
    }
}
