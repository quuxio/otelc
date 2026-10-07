use super::*;
use opentelemetry_proto::tonic::metrics::v1::{metric, number_data_point};
use std::{
    io::{Read, Write},
    net::TcpListener,
    os::unix::{fs::PermissionsExt, net::UnixStream},
    thread,
};

struct Receiver {
    endpoint: String,
    requests: Arc<Mutex<Vec<ExportMetricsServiceRequest>>>,
    stop: Arc<AtomicBool>,
    worker: Option<thread::JoinHandle<()>>,
}
impl Receiver {
    fn new(response: &'static [u8], delay: Duration) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let endpoint = format!("http://{}/v1/metrics", listener.local_addr().unwrap());
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let (received, stopping) = (requests.clone(), stop.clone());
        let worker = thread::spawn(move || {
            while !stopping.load(Ordering::Acquire) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        stream.set_nonblocking(false).unwrap();
                        stream
                            .set_read_timeout(Some(Duration::from_secs(2)))
                            .unwrap();
                        let mut header = Vec::new();
                        let mut byte = [0];
                        while !header.ends_with(b"\r\n\r\n") {
                            if stream.read_exact(&mut byte).is_err() {
                                return;
                            }
                            header.push(byte[0]);
                        }
                        let header = String::from_utf8(header).unwrap();
                        assert!(header.starts_with("POST /v1/metrics HTTP/1.1"));
                        let length: usize = header
                            .lines()
                            .find_map(|line| {
                                line.to_lowercase()
                                    .strip_prefix("content-length:")
                                    .map(|value| value.trim().parse().unwrap())
                            })
                            .unwrap();
                        let mut body = vec![0; length];
                        stream.read_exact(&mut body).unwrap();
                        received
                            .lock()
                            .unwrap()
                            .push(ExportMetricsServiceRequest::decode(body.as_slice()).unwrap());
                        thread::sleep(delay);
                        let _ = stream.write_all(response);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(1))
                    }
                    Err(error) => panic!("{error}"),
                }
            }
        });
        Self {
            endpoint,
            requests,
            stop,
            worker: Some(worker),
        }
    }
    fn good() -> Self {
        Self::new(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-protobuf\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", Duration::ZERO)
    }
    fn wait(&self, number: usize) {
        let deadline = Instant::now() + Duration::from_secs(3);
        while self.requests.lock().unwrap().len() < number {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(2));
        }
    }
}
impl Drop for Receiver {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        self.worker.take().unwrap().join().unwrap();
    }
}
fn plan(receiver: &Receiver) -> policy::Plan {
    let config: quux_otelc_config::common::CommonConfig = toml::from_str("schema_version=2\nlanguages=['rust']\n[resource]\nservice_name='rust-test'\n[export]\ninterval_ms=60000\ntimeout_ms=1000\n[runtime]\nshutdown_timeout_ms=2000\n").unwrap();
    let value = serde_json::to_value(
        config
            .resolve(quux_otelc_config::common::Language::Rust)
            .unwrap(),
    )
    .unwrap();
    let mut plan: policy::Plan = serde_json::from_value(value).unwrap();
    plan.metrics_endpoint = receiver.endpoint.clone();
    plan
}
fn number(request: &ExportMetricsServiceRequest, name: &str) -> u64 {
    request
        .resource_metrics
        .iter()
        .flat_map(|resource| &resource.scope_metrics)
        .flat_map(|scope| &scope.metrics)
        .filter(|m| m.name == name)
        .map(|m| match m.data.as_ref().unwrap() {
            metric::Data::Sum(sum) => sum
                .data_points
                .iter()
                .map(|point| match point.value.as_ref().unwrap() {
                    number_data_point::Value::AsInt(value) => *value as u64,
                    _ => panic!("integer count required"),
                })
                .sum::<u64>(),
            metric::Data::Histogram(histogram) => histogram
                .data_points
                .iter()
                .map(|point| point.count)
                .sum::<u64>(),
            _ => panic!("unexpected metric"),
        })
        .sum::<u64>()
}
#[test]
fn sdk_metrics_preserve_returns_payloads_recursion_and_threads() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(plan(&receiver)).unwrap();
    fn recursive(runtime: &Arc<Runtime>, depth: usize) -> usize {
        let _guard = runtime.enter("recursive");
        if depth == 0 {
            0
        } else {
            1 + recursive(runtime, depth - 1)
        }
    }
    assert_eq!(recursive(&runtime, 3), 3);
    let payload = std::panic::catch_unwind(AssertUnwindSafe(|| {
        let _guard = runtime.enter("escaping");
        std::panic::panic_any(String::from("same payload"));
    }))
    .unwrap_err();
    assert_eq!(payload.downcast_ref::<String>().unwrap(), "same payload");
    {
        let _guard = runtime.enter("caught");
        assert!(std::panic::catch_unwind(|| panic!("caught inside")).is_err());
    }
    let workers: Vec<_> = (0..4)
        .map(|_| {
            let runtime = runtime.clone();
            thread::spawn(move || {
                for _ in 0..20 {
                    let _guard = runtime.enter("threaded");
                }
            })
        })
        .collect();
    for worker in workers {
        worker.join().unwrap();
    }
    runtime.close();
    runtime.close();
    receiver.wait(1);
    let report = runtime.report();
    assert_eq!(report["function_calls"], 86);
    assert_eq!(report["functions"]["escaping"]["unwinds"], 1);
    assert_eq!(report["functions"]["caught"]["unwinds"], 0);
    assert_eq!(report["export_loss"], 0);
    assert_eq!(report["export_finished"], true);
    let requests = receiver.requests.lock().unwrap();
    let request = requests.last().unwrap();
    assert_eq!(number(request, "otelc.function.calls"), 86);
    assert_eq!(number(request, "otelc.function.duration"), 86);
    assert_eq!(number(request, "otelc.function.unwinds"), 1);
    assert!(request.resource_metrics[0]
        .resource
        .as_ref()
        .unwrap()
        .attributes
        .iter()
        .any(|key| key.key == "service.instance.id"));
}
#[test]
fn guards_entered_during_an_existing_unwind_are_normal_completions() {
    struct Cleanup(Arc<Runtime>);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _guard = self.0.enter("Drop");
        }
    }
    let receiver = Receiver::good();
    let runtime = Runtime::new(plan(&receiver)).unwrap();
    assert!(catch_unwind(AssertUnwindSafe(|| {
        let _cleanup = Cleanup(runtime.clone());
        let _guard = runtime.enter("body");
        panic!("unwind");
    }))
    .is_err());
    runtime.close();
    let report = runtime.report();
    assert_eq!(report["functions"]["body"]["unwinds"], 1);
    assert_eq!(report["functions"]["Drop"]["unwinds"], 0);
    assert_eq!(report["function_calls"], 2);
}

#[test]
fn async_completion_cancellation_and_panic_have_distinct_sdk_counters() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(plan(&receiver)).unwrap();
    let context = &mut std::task::Context::from_waker(std::task::Waker::noop());
    let mut pending = Box::pin(async {
        observe_with_guard(runtime.enter("cancelled"), std::future::pending::<()>()).await;
    });
    assert!(std::future::Future::poll(pending.as_mut(), context).is_pending());
    drop(pending);
    let mut completed = Box::pin(async {
        observe_with_guard(runtime.enter("completed"), std::future::ready(23)).await
    });
    assert_eq!(
        std::future::Future::poll(completed.as_mut(), context),
        std::task::Poll::Ready(23)
    );
    assert!(catch_unwind(AssertUnwindSafe(|| {
        let mut panicked = Box::pin(async {
            observe_with_guard(runtime.enter("panicked"), async {
                panic!("original panic")
            })
            .await
        });
        let _ = std::future::Future::poll(panicked.as_mut(), context);
    }))
    .is_err());
    runtime.close();
    receiver.wait(1);
    let report = runtime.report();
    assert_eq!(report["function_calls"], 3);
    assert_eq!(report["functions"]["cancelled"]["cancellations"], 1);
    assert_eq!(report["functions"]["cancelled"]["unwinds"], 0);
    assert_eq!(report["functions"]["panicked"]["cancellations"], 0);
    assert_eq!(report["functions"]["panicked"]["unwinds"], 1);
    let requests = receiver.requests.lock().unwrap();
    let request = requests.last().unwrap();
    assert_eq!(number(request, "otelc.function.calls"), 3);
    assert_eq!(number(request, "otelc.function.duration"), 3);
    assert_eq!(number(request, "otelc.function.cancellations"), 1);
    assert_eq!(number(request, "otelc.function.unwinds"), 1);
}

#[test]
fn async_admission_is_fixed_at_first_poll_bounded_and_drained_at_shutdown() {
    let receiver = Receiver::good();
    let mut config = plan(&receiver);
    config.runtime.max_active_calls = 1;
    let runtime = Runtime::new(config).unwrap();
    let context = &mut std::task::Context::from_waker(std::task::Waker::noop());
    let observed = &runtime;
    let future = |name| async move {
        observe_with_guard(observed.enter(name), std::future::pending::<()>()).await;
    };
    runtime.state.enabled.store(false, Ordering::Release);
    let mut disabled = Box::pin(future("disabled"));
    assert!(std::future::Future::poll(disabled.as_mut(), context).is_pending());
    runtime.state.enabled.store(true, Ordering::Release);
    assert!(std::future::Future::poll(disabled.as_mut(), context).is_pending());
    let mut admitted = Box::pin(future("admitted"));
    assert!(std::future::Future::poll(admitted.as_mut(), context).is_pending());
    let mut rejected = Box::pin(future("rejected"));
    assert!(std::future::Future::poll(rejected.as_mut(), context).is_pending());
    runtime.state.enabled.store(false, Ordering::Release);
    drop(admitted);
    drop(rejected);
    drop(disabled);
    runtime.state.enabled.store(true, Ordering::Release);
    let mut pending = Box::pin(future("pending"));
    assert!(std::future::Future::poll(pending.as_mut(), context).is_pending());
    runtime.close();
    drop(pending);
    let report = runtime.report();
    assert_eq!(report["function_calls"], 1);
    assert_eq!(report["functions"]["admitted"]["cancellations"], 1);
    assert!(report["functions"].get("disabled").is_none());
    assert_eq!(report["losses"]["active_call_capacity"], 1);
    assert_eq!(report["losses"]["incomplete"], 1);
    assert_eq!(report["export_loss"], 0);
}
fn request(path: &std::path::Path, command: &[u8]) -> serde_json::Value {
    let mut stream = UnixStream::connect(path).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    stream.write_all(command).unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).unwrap();
    serde_json::from_str(&response).unwrap()
}
#[test]
fn private_controls_keep_inflight_tokens_and_disabled_calls_unmeasured() {
    let directory = tempfile::Builder::new()
        .prefix("ru-")
        .tempdir_in("/tmp")
        .unwrap();
    std::fs::set_permissions(directory.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = directory.path().join("s");
    let receiver = Receiver::good();
    let mut policy = plan(&receiver);
    policy.runtime.control_socket = Some(path.to_string_lossy().into());
    let runtime = Runtime::new(policy).unwrap();
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let first = runtime.enter("admitted");
    assert_eq!(request(&path, b"disable\n")["metrics_enabled"], false);
    let disabled = runtime.enter("disabled");
    assert_eq!(request(&path, b"enable\n")["metrics_enabled"], true);
    drop(disabled);
    drop(first);
    assert_eq!(request(&path, b"status\n")["function_calls"], 1);
    assert_eq!(
        request(&path, b"bogus\n")["error"],
        "invalid control request"
    );
    let mut unfinished = UnixStream::connect(&path).unwrap();
    unfinished
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    unfinished.write_all(b"status").unwrap();
    let started = Instant::now();
    let mut response = String::new();
    unfinished.read_to_string(&mut response).unwrap();
    assert!(started.elapsed() < Duration::from_secs(1));
    assert!(response.contains("invalid control request"));
    let abandoned = runtime.enter("abandoned");
    runtime.close();
    drop(abandoned);
    let _after = runtime.enter("after");
    assert!(!path.exists());
    assert_eq!(runtime.report()["losses"]["incomplete"], 1);
    assert_eq!(runtime.report()["function_calls"], 1);
}
#[test]
fn control_shutdown_preserves_a_replacement_at_the_same_path() {
    let directory = tempfile::Builder::new()
        .prefix("ru-")
        .tempdir_in("/tmp")
        .unwrap();
    std::fs::set_permissions(directory.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = directory.path().join("s");
    let receiver = Receiver::good();
    let mut policy = plan(&receiver);
    policy.runtime.control_socket = Some(path.to_string_lossy().into());
    let runtime = Runtime::new(policy).unwrap();
    std::fs::remove_file(&path).unwrap();
    std::fs::write(&path, "preserve replacement").unwrap();
    runtime.close();
    assert_eq!(
        std::fs::read_to_string(&path).unwrap(),
        "preserve replacement"
    );
}
#[test]
fn bounded_capacity_and_periodic_loss_export_are_observable() {
    let receiver = Receiver::good();
    let mut policy = plan(&receiver);
    policy.runtime.max_functions = 1;
    policy.runtime.max_active_calls = 1;
    policy.export.interval_ms = 20;
    let runtime = Runtime::new(policy).unwrap();
    let first = runtime.enter("one");
    let _second = runtime.enter("one");
    let _third = runtime.enter("two");
    receiver.wait(1);
    assert_eq!(runtime.report()["losses"]["active_call_capacity"], 1);
    assert_eq!(runtime.report()["losses"]["function_capacity"], 1);
    runtime.state.data.lock().unwrap().next = u64::MAX;
    drop(first);
    let _overflow = runtime.enter("one");
    runtime.close();
    assert_eq!(runtime.report()["losses"]["invalid"], 1);
    assert!(receiver
        .requests
        .lock()
        .unwrap()
        .iter()
        .any(|request| number(request, "otelc.runtime.dropped_observations") >= 2));
}
#[test]
fn rejection_and_shutdown_deadlines_are_visible() {
    let rejected = Receiver::new(
        b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        Duration::ZERO,
    );
    let runtime = Runtime::new(plan(&rejected)).unwrap();
    drop(runtime.enter("one"));
    runtime.close();
    assert!(runtime.report()["export_loss"].as_u64().unwrap() > 0);
    let stalled = Receiver::new(b"", Duration::from_millis(600));
    let mut policy = plan(&stalled);
    policy.export.timeout_ms = 100;
    policy.runtime.shutdown_timeout_ms = 200;
    let runtime = Runtime::new(policy).unwrap();
    drop(runtime.enter("one"));
    let started = Instant::now();
    runtime.close();
    assert!(started.elapsed() < Duration::from_millis(450));
    assert!(runtime.report()["export_loss"].as_u64().unwrap() > 0);
}
#[test]
fn policy_and_control_fail_before_application_launch_without_destroying_files() {
    let receiver = Receiver::good();
    let directory = tempfile::tempdir().unwrap();
    let file = directory.path().join("plan.json");
    let valid = serde_json::to_value(
        quux_otelc_config::common::CommonConfig::load(
            std::path::Path::new("../../examples/rust.toml"),
            true,
        )
        .unwrap()
        .resolve(quux_otelc_config::common::Language::Rust)
        .unwrap(),
    )
    .unwrap();
    std::fs::write(&file, serde_json::to_vec(&valid).unwrap()).unwrap();
    assert!(policy::Plan::load(&file).is_ok());
    for (key, value) in [
        ("language", serde_json::json!("go")),
        ("execution_available", serde_json::json!(false)),
    ] {
        let mut invalid = valid.clone();
        invalid[key] = value;
        std::fs::write(&file, invalid.to_string()).unwrap();
        assert!(policy::Plan::load(&file).is_err());
    }
    let mut invalid = valid;
    invalid["runtime"]["max_functions"] = serde_json::json!(0);
    std::fs::write(&file, invalid.to_string()).unwrap();
    assert!(policy::Plan::load(&file).is_err());
    let path = directory.path().join("occupied");
    std::fs::write(&path, "preserve me").unwrap();
    let mut policy = plan(&receiver);
    policy.runtime.control_socket = Some(path.to_string_lossy().into());
    assert!(Runtime::new(policy.clone()).is_err());
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "preserve me");
    std::fs::remove_file(&path).unwrap();
    std::fs::set_permissions(directory.path(), std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(Runtime::new(policy).is_err());
}
