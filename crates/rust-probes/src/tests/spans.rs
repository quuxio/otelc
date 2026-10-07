use super::*;
use opentelemetry_proto::tonic::trace::v1::Span;
use std::{
    future::Future,
    pin::Pin,
    task::{Context, Poll},
};
fn trace_plan(receiver: &Receiver) -> policy::Plan {
    let mut plan = plan(receiver);
    plan.traces.enabled = true;
    plan.traces.root_sample_ratio = 1.0;
    plan.trace_export = Some(quux_otelc_config::common::TraceExport {
        endpoint: receiver.endpoint.replace("/v1/metrics", "/v1/traces"),
        protocol: "http/protobuf".into(),
        timeout_ms: 1000,
    });
    plan
}
fn spans(receiver: &Receiver) -> Vec<Span> {
    receiver
        .traces
        .lock()
        .unwrap()
        .iter()
        .flat_map(|request| &request.resource_spans)
        .flat_map(|resource| &resource.scope_spans)
        .flat_map(|scope| scope.spans.clone())
        .collect()
}
fn poll<F: Future>(future: Pin<&mut F>) -> Poll<F::Output> {
    future.poll(&mut Context::from_waker(std::task::Waker::noop()))
}
struct YieldOnce(bool);
impl Future for YieldOnce {
    type Output = ();
    fn poll(mut self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<()> {
        if self.0 {
            Poll::Ready(())
        } else {
            self.0 = true;
            Poll::Pending
        }
    }
}
#[test]
fn decoded_otlp_tree_has_resource_scope_valid_ids_and_nested_timestamps() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(trace_plan(&receiver)).unwrap();
    fn recurse(runtime: &Arc<Runtime>, depth: usize) -> usize {
        let _scope = runtime.enter_scoped("recursive");
        if depth == 0 {
            0
        } else {
            1 + recurse(runtime, depth - 1)
        }
    }
    assert_eq!(recurse(&runtime, 3), 3);
    assert!(traces::current(runtime.owner).is_none());
    runtime.close();
    assert_eq!(runtime.report()["function_calls"], 4);
    assert_eq!(runtime.report()["export_loss"], 0);
    let spans = spans(&receiver);
    assert_eq!(spans.len(), 4);
    for (index, span) in spans.iter().enumerate() {
        assert_eq!(span.trace_id.len(), 16);
        assert_eq!(span.span_id.len(), 8);
        assert!(span.span_id.iter().any(|byte| *byte != 0));
        assert_eq!(span.trace_id, spans[0].trace_id);
        assert!(span.end_time_unix_nano >= span.start_time_unix_nano);
        if index == 0 {
            assert!(span.parent_span_id.is_empty());
        } else {
            assert_eq!(span.parent_span_id, spans[index - 1].span_id);
            assert!(span.start_time_unix_nano >= spans[index - 1].start_time_unix_nano);
            assert!(span.end_time_unix_nano <= spans[index - 1].end_time_unix_nano);
        }
    }
    let requests = receiver.traces.lock().unwrap();
    let resource = &requests[0].resource_spans[0];
    assert!(resource
        .resource
        .as_ref()
        .unwrap()
        .attributes
        .iter()
        .any(|attr| attr.key == "service.name"));
    assert_eq!(
        resource.scope_spans[0].scope.as_ref().unwrap().name,
        "quux.otelc"
    );
}
#[test]
fn traces_work_with_metrics_disabled_without_changing_metric_admission() {
    let receiver = Receiver::good();
    let mut plan = trace_plan(&receiver);
    plan.metrics.enabled = false;
    let runtime = Runtime::new(plan).unwrap();
    let admitted = runtime.enter_scoped("root");
    {
        let _child = runtime.enter_scoped("child");
    }
    runtime.state.enabled.store(true, Ordering::Release);
    drop(admitted);
    drop(runtime.enter_scoped("metrics_on"));
    runtime.close();
    assert_eq!(spans(&receiver).len(), 3);
    assert_eq!(runtime.report()["function_calls"], 1);
    assert_eq!(runtime.report()["functions"]["root"]["count"], 0);
    assert_eq!(
        number(
            receiver.requests.lock().unwrap().last().unwrap(),
            "otelc.function.calls"
        ),
        1
    );
}
#[test]
fn oversized_complete_tree_is_discarded_and_its_loss_reaches_periodic_metrics() {
    let receiver = Receiver::good();
    let mut plan = trace_plan(&receiver);
    plan.metrics.enabled = false;
    plan.export.interval_ms = 10;
    plan.traces.max_active_traces = 1;
    plan.traces.max_spans_per_trace = 10000;
    let runtime = Runtime::new(plan).unwrap();
    let root = runtime.enter_scoped("root");
    let name = "x".repeat(1024);
    for _ in 0..8192 {
        drop(runtime.enter_scoped(&name));
    }
    drop(root);
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if receiver
            .requests
            .lock()
            .unwrap()
            .iter()
            .any(|request| number(request, "otelc.trace.dropped_trees") == 1)
        {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "trace loss was not exported periodically"
        );
        thread::sleep(Duration::from_millis(2));
    }
    assert!(spans(&receiver).is_empty());
    assert_eq!(runtime.report()["traces"]["losses"]["batch_bytes"], 1);
    runtime.close();
}
#[test]
fn async_context_survives_thread_migration_and_restores_each_poll() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(trace_plan(&receiver)).unwrap();
    let observed = runtime.clone();
    let mut future = Box::pin(async move {
        observe_with_guard(observed.enter("parent"), async {
            observe_with_guard(observed.enter("child"), async {
                YieldOnce(false).await;
                42
            })
            .await
        })
        .await
    });
    assert!(poll(future.as_mut()).is_pending());
    assert!(traces::current(runtime.owner).is_none());
    let owner = runtime.owner;
    let future = thread::spawn(move || {
        assert_eq!(poll(future.as_mut()), Poll::Ready(42));
        assert!(traces::current(owner).is_none());
        future
    })
    .join()
    .unwrap();
    drop(future);
    runtime.close();
    let spans = spans(&receiver);
    assert_eq!(spans.len(), 2);
    assert_eq!(spans[1].parent_span_id, spans[0].span_id);
    assert_eq!(runtime.report()["export_loss"], 0);
}
struct OnDrop(Arc<Runtime>);
impl Drop for OnDrop {
    fn drop(&mut self) {
        drop(self.0.enter_scoped("cleanup"));
    }
}
#[test]
fn cancellation_drop_cleanup_inherits_context_and_unpolled_futures_have_no_spans() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(trace_plan(&receiver)).unwrap();
    let observed = runtime.clone();
    let mut pending = Box::pin(async move {
        observe_with_guard(observed.enter("cancel"), async {
            let _cleanup = OnDrop(observed.clone());
            std::future::pending::<()>().await;
        })
        .await
    });
    assert!(poll(pending.as_mut()).is_pending());
    drop(pending);
    let never = runtime.clone();
    drop(async move { observe_with_guard(never.enter("unpolled"), async {}).await });
    runtime.close();
    let spans = spans(&receiver);
    assert_eq!(spans.len(), 2);
    let parent = spans.iter().find(|span| span.name == "cancel").unwrap();
    let cleanup = spans.iter().find(|span| span.name == "cleanup").unwrap();
    assert_eq!(cleanup.parent_span_id, parent.span_id);
    assert_eq!(parent.status.as_ref().unwrap().code, 2);
    assert_eq!(runtime.report()["functions"]["cancel"]["cancellations"], 1);
}
#[test]
fn escaping_panics_preserve_payload_and_caught_panics_do_not_mark_parent_error() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(trace_plan(&receiver)).unwrap();
    {
        let _caught = runtime.enter_scoped("caught");
        assert!(std::panic::catch_unwind(|| panic!("caught")).is_err());
    }
    let panic = std::panic::catch_unwind(AssertUnwindSafe(|| {
        let _outer = runtime.enter_scoped("outer");
        let _inner = runtime.enter_scoped("inner");
        std::panic::panic_any(String::from("original"));
    }))
    .unwrap_err();
    assert_eq!(*panic.downcast::<String>().unwrap(), "original");
    assert!(traces::current(runtime.owner).is_none());
    runtime.close();
    let spans = spans(&receiver);
    assert_eq!(spans.len(), 3);
    assert!(spans
        .iter()
        .find(|span| span.name == "caught")
        .unwrap()
        .status
        .as_ref()
        .is_none_or(|status| status.code == 0));
    assert_eq!(
        spans
            .iter()
            .filter(|span| span.status.as_ref().is_some_and(|status| status.code == 2))
            .count(),
        2
    );
}
#[test]
fn shared_call_function_and_span_limits_discard_whole_trees() {
    for reason in ["function_capacity", "active_call_capacity", "span_capacity"] {
        let receiver = Receiver::good();
        let mut plan = trace_plan(&receiver);
        match reason {
            "function_capacity" => plan.runtime.max_functions = 1,
            "active_call_capacity" => plan.runtime.max_active_calls = 1,
            _ => plan.traces.max_spans_per_trace = 1,
        }
        let runtime = Runtime::new(plan).unwrap();
        let root = runtime.enter_scoped("root");
        drop(runtime.enter_scoped("child"));
        drop(root);
        runtime.close();
        assert!(spans(&receiver).is_empty());
        assert_eq!(runtime.report()["traces"]["losses"][reason], 1);
    }
}
#[test]
fn shutdown_discards_pending_tree_but_exports_previously_completed_tree() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(trace_plan(&receiver)).unwrap();
    drop(runtime.enter_scoped("completed"));
    let pending = runtime.enter_scoped("pending");
    runtime.close();
    drop(pending);
    assert_eq!(spans(&receiver).len(), 1);
    assert_eq!(runtime.report()["traces"]["losses"]["incomplete"], 1);
    assert_eq!(runtime.report()["traces"]["active_trees"], 0);
}
#[test]
fn trace_collector_rejection_is_visible_and_shutdown_is_bounded() {
    let rejected = Receiver::new(
        b"HTTP/1.1 400 Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        Duration::ZERO,
    );
    let runtime = Runtime::new(trace_plan(&rejected)).unwrap();
    drop(runtime.enter_scoped("one"));
    runtime.close();
    assert_eq!(rejected.traces.lock().unwrap().len(), 1);
    assert!(runtime.report()["export_loss"].as_u64().unwrap() > 0);
    let metrics = Receiver::good();
    let stalled = Receiver::new(b"", Duration::from_millis(500));
    let mut plan = trace_plan(&metrics);
    plan.trace_export.as_mut().unwrap().endpoint =
        stalled.endpoint.replace("/v1/metrics", "/v1/traces");
    plan.runtime.shutdown_timeout_ms = 100;
    let runtime = Runtime::new(plan).unwrap();
    drop(runtime.enter_scoped("one"));
    let start = Instant::now();
    runtime.close();
    assert!(start.elapsed() < Duration::from_millis(350));
    assert!(runtime.report()["export_loss"].as_u64().unwrap() > 0);
}
#[test]
fn forged_trace_plans_are_rejected_before_a_worker_or_control_is_started() {
    let receiver = Receiver::good();
    let mut plan = trace_plan(&receiver);
    plan.trace_export = None;
    assert!(Runtime::new(plan).is_err());
    for invalid in 0..9 {
        let mut plan = trace_plan(&receiver);
        match invalid {
            0 => plan.traces.root_sample_ratio = f64::NAN,
            1 => plan.traces.root_sample_ratio = 2.0,
            2 => plan.traces.max_active_traces = 0,
            3 => plan.traces.max_spans_per_trace = 0,
            4 => {
                plan.traces.max_active_traces = 65536;
                plan.traces.max_spans_per_trace = 65536
            }
            5 => plan.trace_export.as_mut().unwrap().timeout_ms = 0,
            6 => plan.trace_export.as_mut().unwrap().protocol = "grpc".into(),
            7 => plan.trace_export.as_mut().unwrap().endpoint = "http://remote.example".into(),
            _ => plan.export.max_queued_batches = 0,
        }
        assert!(Runtime::new(plan).is_err());
    }
}
#[test]
fn unsampled_trace_only_calls_have_no_pending_observations_or_shutdown_loss() {
    let receiver = Receiver::good();
    let mut plan = trace_plan(&receiver);
    plan.metrics.enabled = false;
    plan.traces.root_sample_ratio = 0.0;
    let runtime = Runtime::new(plan).unwrap();
    let root = runtime.enter_scoped("root");
    let child = runtime.enter_scoped("child");
    runtime.close();
    drop(child);
    drop(root);
    assert!(spans(&receiver).is_empty());
    assert_eq!(runtime.report()["losses"]["incomplete"], 0);
    assert_eq!(runtime.report()["traces"]["sampled_out_roots"], 1);
    assert_eq!(runtime.report()["traces"]["losses"], serde_json::json!({}));
}
