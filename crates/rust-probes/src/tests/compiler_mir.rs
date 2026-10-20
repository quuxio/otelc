use super::*;
use crate::tests::{
    spans::{spans, trace_plan},
    Receiver,
};

fn key(address: usize, site: u128) -> Key {
    Key { address, site }
}
fn context(runtime: &Runtime) -> String {
    format!("{:?}", traces::current(runtime.owner))
}

#[test]
fn repeated_polls_restore_nested_context_and_keep_late_children_parented() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(trace_plan(&receiver)).unwrap();
    let parent = runtime.enter_future("parent", key(16, 1));
    let parent_context = context(&runtime);
    let child = runtime.enter_future("child", key(16, 2));
    let child_context = context(&runtime);
    assert_ne!(parent_context, child_context);
    poll_exit(child, false);
    assert_eq!(context(&runtime), parent_context);
    poll_exit(parent, true);
    assert!(traces::current(runtime.owner).is_none());
    let other = runtime.enter_scoped("independent");
    let other_context = context(&runtime);
    let resumed = runtime.enter_future("child", key(16, 2));
    assert_eq!(context(&runtime), child_context);
    poll_exit(resumed, false);
    assert_eq!(context(&runtime), other_context);
    poll_exit(runtime.enter_future("child", key(16, 2)), true);
    assert_eq!(context(&runtime), other_context);
    drop(other);
    assert!(traces::current(runtime.owner).is_none());
    assert_eq!(runtime.report()["future_observers"]["pending"], 0);
    runtime.close();
    assert_eq!(runtime.report()["function_calls"], 3);
    assert_eq!(runtime.report()["export_loss"], 0);
    let decoded = spans(&receiver);
    assert_eq!(decoded.len(), 3);
    let find = |name| decoded.iter().find(|span| span.name == name).unwrap();
    let (parent, child, other) = (find("parent"), find("child"), find("independent"));
    assert_eq!(child.parent_span_id, parent.span_id);
    assert_eq!(child.trace_id, parent.trace_id);
    assert_ne!(other.trace_id, parent.trace_id);
    assert!(child.end_time_unix_nano >= parent.end_time_unix_nano);
}

#[test]
fn cross_thread_drop_and_escaping_panic_complete_once_and_preserve_payloads() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(trace_plan(&receiver)).unwrap();
    drop_exit(runtime.drop_future(key(99, 1)));
    poll_exit(runtime.enter_future("cancelled", key(16, 1)), false);
    let worker = runtime.clone();
    std::thread::spawn(move || {
        assert!(traces::current(worker.owner).is_none());
        let dropping = worker.drop_future(key(16, 1));
        let cleanup = worker.enter_scoped("cleanup");
        drop(cleanup);
        drop_exit(dropping);
        assert!(traces::current(worker.owner).is_none());
        drop_exit(worker.drop_future(key(16, 1)));
    })
    .join()
    .unwrap();
    let payload = std::panic::catch_unwind(AssertUnwindSafe(|| {
        let guard = runtime.enter_future("panicking", key(16, 2));
        let _guard = guard;
        std::panic::panic_any(String::from("original payload"));
    }))
    .unwrap_err();
    assert_eq!(
        payload.downcast_ref::<String>().unwrap(),
        "original payload"
    );
    assert!(traces::current(runtime.owner).is_none());
    assert_eq!(runtime.report()["future_observers"]["pending"], 0);
    runtime.close();
    let report = runtime.report();
    assert_eq!(report["functions"]["cancelled"]["cancellations"], 1);
    assert_eq!(report["functions"]["panicking"]["unwinds"], 1);
    assert_eq!(report["functions"]["panicking"]["cancellations"], 0);
    let decoded = spans(&receiver);
    assert_eq!(decoded.len(), 3);
    let cancelled = decoded
        .iter()
        .find(|span| span.name == "cancelled")
        .unwrap();
    let cleanup = decoded.iter().find(|span| span.name == "cleanup").unwrap();
    assert_eq!(cleanup.parent_span_id, cancelled.span_id);
    assert_eq!(cleanup.trace_id, cancelled.trace_id);
}

#[test]
fn metrics_admission_is_sticky_and_completed_identity_storage_can_be_reused() {
    let receiver = Receiver::good();
    let mut policy = crate::tests::plan(&receiver);
    policy.metrics.enabled = false;
    policy.runtime.max_active_calls = 1;
    let runtime = Runtime::new(policy).unwrap();
    poll_exit(runtime.enter_future("disabled", key(16, 1)), false);
    runtime.state.enabled.store(true, Ordering::Release);
    poll_exit(runtime.enter_future("disabled", key(16, 1)), true);
    assert_eq!(runtime.report()["function_calls"], 0);
    poll_exit(runtime.enter_future("enabled", key(16, 1)), false);
    let token = runtime.futures.lock().unwrap().entries[&key(16, 1)].token;
    runtime.state.enabled.store(false, Ordering::Release);
    poll_exit(runtime.enter_future("enabled", key(16, 1)), true);
    runtime.state.enabled.store(true, Ordering::Release);
    let reused = runtime.enter_future("enabled", key(16, 1));
    assert_ne!(
        runtime.futures.lock().unwrap().entries[&key(16, 1)].token,
        token
    );
    poll_exit(reused, true);
    assert_eq!(
        runtime.report()["future_observers"]["admission_exhausted"],
        false
    );
    runtime.close();
    assert_eq!(runtime.report()["function_calls"], 2);
    assert_eq!(runtime.report()["losses"]["incomplete"], 0);
}

#[test]
fn rejected_first_poll_cannot_become_a_new_sample_after_active_capacity_is_freed() {
    let receiver = Receiver::good();
    let mut policy = trace_plan(&receiver);
    policy.metrics.enabled = false;
    policy.runtime.max_active_calls = 1;
    let runtime = Runtime::new(policy).unwrap();
    let occupied = runtime.enter_scoped("occupied");
    let rejected = runtime.enter_future("rejected", key(16, 1));
    assert!(!traces::current(runtime.owner).unwrap().sampled);
    poll_exit(rejected, false);
    drop(occupied);
    let resumed = runtime.enter_future("rejected", key(16, 1));
    assert!(!traces::current(runtime.owner).unwrap().sampled);
    let child = runtime.enter_scoped("inside_rejected");
    drop(child);
    poll_exit(resumed, true);
    poll_exit(runtime.enter_future("healthy", key(16, 1)), true);
    assert_eq!(
        runtime.report()["future_observers"]["admission_exhausted"],
        false
    );
    runtime.close();
    let decoded = spans(&receiver);
    assert_eq!(decoded.len(), 1);
    assert_eq!(decoded[0].name, "healthy");
    assert_eq!(runtime.report()["losses"]["active_call_capacity"], 1);
}

#[test]
fn exhausted_identity_storage_fails_closed_and_shutdown_censors_pending_frames() {
    let receiver = Receiver::good();
    let mut policy = trace_plan(&receiver);
    policy.metrics.enabled = false;
    policy.runtime.max_active_calls = 1;
    let runtime = Runtime::new(policy).unwrap();
    poll_exit(runtime.enter_future("known", key(16, 1)), false);
    poll_exit(runtime.enter_future("unknown", key(32, 2)), false);
    poll_exit(runtime.enter_future("known", key(16, 1)), true);
    let rejected = runtime.enter_future("unknown", key(32, 2));
    assert!(!traces::current(runtime.owner).unwrap().sampled);
    poll_exit(rejected, true);
    drop_exit(runtime.drop_future(key(32, 2)));
    assert_eq!(runtime.report()["future_observers"]["pending"], 0);
    assert_eq!(
        runtime.report()["future_observers"]["admission_exhausted"],
        true
    );
    runtime.close();
    assert_eq!(spans(&receiver).len(), 1);
    assert_eq!(runtime.report()["losses"]["future_capacity"], 3);
    let receiver = Receiver::good();
    let runtime = Runtime::new(trace_plan(&receiver)).unwrap();
    poll_exit(
        runtime.enter_future("pending_at_shutdown", key(16, 1)),
        false,
    );
    assert_eq!(runtime.report()["losses"]["incomplete"], 1);
    runtime.close();
    poll_exit(runtime.enter_future("closed", key(16, 1)), true);
    drop_exit(runtime.drop_future(key(16, 1)));
    assert_eq!(runtime.report()["future_observers"]["pending"], 0);
    assert_eq!(runtime.report()["losses"]["incomplete"], 1);
    assert_eq!(runtime.report()["function_calls"], 0);
    assert!(spans(&receiver).is_empty());
}

#[test]
fn invalid_inputs_and_uninstalled_public_hooks_are_inert() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(trace_plan(&receiver)).unwrap();
    for (name, identity) in [
        ("valid", key(0, 1)),
        ("valid", key(16, 0)),
        ("", key(16, 1)),
    ] {
        poll_exit(runtime.enter_future(name, identity), true);
    }
    poll_exit(runtime.enter_future(&"x".repeat(1025), key(16, 1)), true);
    poll_exit(poll_enter("not_installed", 1, 16), true);
    drop_exit(drop_enter(1, 16));
    poll_unwind(PollGuard::inactive());
    sync_exit(sync_enter("not_installed"));
    assert_eq!(runtime.report()["losses"]["invalid"], 4);
    assert_eq!(runtime.report()["future_observers"]["pending"], 0);
    runtime.close();
    assert_eq!(runtime.report()["function_calls"], 0);
}

#[test]
fn sdk_operations_do_not_reenter_sync_or_future_recording() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(trace_plan(&receiver)).unwrap();
    let parent = runtime.enter_scoped("application");
    let original = context(&runtime);
    poll_exit(runtime.enter_future("pending", key(16, 1)), false);
    {
        let _sdk = crate::recording::Suppression::enter();
        drop(runtime.enter_scoped("sdk_allocation"));
        poll_exit(runtime.enter_future("sdk_poll", key(32, 2)), true);
        drop_exit(runtime.drop_future(key(16, 1)));
    }
    assert_eq!(context(&runtime), original);
    assert_eq!(runtime.report()["future_observers"]["pending"], 1);
    poll_exit(runtime.enter_future("pending", key(16, 1)), true);
    drop(parent);
    runtime.close();
    let report = runtime.report();
    assert_eq!(report["function_calls"], 2);
    assert_eq!(report["future_observers"]["pending"], 0);
    assert_eq!(report["export_loss"], 0);
    let names: Vec<_> = spans(&receiver).into_iter().map(|span| span.name).collect();
    assert_eq!(names.len(), 2);
    assert!(names
        .iter()
        .all(|name| name == "application" || name == "pending"));
}
