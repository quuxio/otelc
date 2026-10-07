//! Source-free Rust body guards and language-native SDK metrics.
mod control;
pub mod policy;
mod reader;
#[cfg(test)]
mod tests;
mod traces;
use anyhow::Result;
use opentelemetry::{
    metrics::{Counter, Histogram, MeterProvider},
    KeyValue,
};
use opentelemetry_proto::tonic::collector::metrics::v1::ExportMetricsServiceRequest;
use opentelemetry_sdk::{
    metrics::{
        data::ResourceMetrics, reader::MetricReader, ManualReader, SdkMeterProvider, Stream,
    },
    Resource,
};
use prost::Message;
use reader::Reader;
use std::{
    collections::{BTreeMap, HashMap},
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc, Arc, Mutex, OnceLock,
    },
    time::{Duration, Instant},
};

struct Function {
    name: Arc<str>,
    count: u64,
    unwinds: u64,
    cancellations: u64,
    attributes: Vec<KeyValue>,
}
struct Frame {
    metrics: bool,
    trace: Option<traces::Context>,
    name: String,
    started: Instant,
    was_panicking: bool,
}
#[derive(Default)]
struct Data {
    functions: BTreeMap<String, Function>,
    pending: HashMap<u64, Frame>,
    next: u64,
    losses: BTreeMap<&'static str, u64>,
    traces: traces::Store,
}
struct State {
    data: Mutex<Data>,
    enabled: AtomicBool,
    closed: AtomicBool,
    revision: AtomicU64,
    export_loss: AtomicU64,
    deadline: Mutex<Option<Instant>>,
    finished: AtomicBool,
}
pub struct Runtime {
    owner: u64,
    plan: policy::Plan,
    state: Arc<State>,
    provider: SdkMeterProvider,
    calls: Counter<u64>,
    unwinds: Counter<u64>,
    cancellations: Counter<u64>,
    duration: Histogram<f64>,
    stop: mpsc::SyncSender<mpsc::SyncSender<()>>,
    control: Mutex<Option<control::Control>>,
}
static NEXT_OWNER: AtomicU64 = AtomicU64::new(0);
static ACTIVE: OnceLock<Arc<Runtime>> = OnceLock::new();
impl Runtime {
    pub fn new(plan: policy::Plan) -> Result<Arc<Self>> {
        plan.validate()?;
        let owner = NEXT_OWNER
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                value.checked_add(1)
            })
            .map_err(|_| anyhow::anyhow!("Rust runtime identity exhausted"))?
            + 1;
        let mut data = Data::default();
        for reason in [
            "function_capacity",
            "active_call_capacity",
            "incomplete",
            "invalid",
        ] {
            data.losses.insert(reason, 0);
        }
        let state = Arc::new(State {
            data: Mutex::new(data),
            enabled: AtomicBool::new(plan.metrics.enabled),
            closed: AtomicBool::new(false),
            revision: AtomicU64::new(0),
            export_loss: AtomicU64::new(0),
            deadline: Mutex::new(None),
            finished: AtomicBool::new(false),
        });
        let reader = Reader(Arc::new(ManualReader::default()));
        let mut attributes = vec![
            KeyValue::new("service.name", plan.resource.service_name.clone()),
            KeyValue::new("service.version", plan.resource.service_version.clone()),
            KeyValue::new("service.instance.id", std::process::id().to_string()),
        ];
        attributes.extend(
            plan.resource
                .attributes
                .iter()
                .map(|(key, value)| KeyValue::new(key.clone(), value.clone())),
        );
        let resource = Resource::builder_empty()
            .with_attributes(attributes)
            .build();
        let trace_resource =
            opentelemetry_proto::transform::common::tonic::ResourceAttributesWithSchema::from(
                &resource,
            );
        let maximum = plan.runtime.max_functions + 1;
        let provider = SdkMeterProvider::builder()
            .with_reader(reader.clone())
            .with_resource(resource)
            .with_view(move |instrument| {
                if instrument.name().starts_with("otelc.function.") {
                    Stream::builder()
                        .with_cardinality_limit(maximum)
                        .build()
                        .ok()
                } else {
                    None
                }
            })
            .build();
        let meter = provider.meter("quux.otelc");
        let calls = meter
            .u64_counter("otelc.function.calls")
            .with_unit("{call}")
            .build();
        let unwinds = meter
            .u64_counter("otelc.function.unwinds")
            .with_unit("{observation}")
            .build();
        let cancellations = meter
            .u64_counter("otelc.function.cancellations")
            .with_unit("{observation}")
            .build();
        let duration = meter
            .f64_histogram("otelc.function.duration")
            .with_unit("s")
            .with_boundaries(plan.metrics.histogram_boundaries_seconds.clone())
            .build();
        let lost = state.clone();
        meter
            .u64_observable_counter("otelc.runtime.dropped_observations")
            .with_callback(move |observer| {
                if let Ok(data) = lost.data.lock() {
                    for (reason, count) in &data.losses {
                        observer.observe(*count, &[KeyValue::new("reason", *reason)]);
                    }
                }
            })
            .build();
        let lost = state.clone();
        meter
            .u64_observable_counter("otelc.export.dropped_batches")
            .with_callback(move |observer| {
                observer.observe(lost.export_loss.load(Ordering::Relaxed), &[])
            })
            .build();
        let lost = state.clone();
        meter
            .u64_observable_counter("otelc.trace.dropped_trees")
            .with_callback(move |observer| {
                if let Ok(data) = lost.data.lock() {
                    for (reason, count) in &data.traces.losses {
                        observer.observe(*count, &[KeyValue::new("reason", *reason)]);
                    }
                }
            })
            .build();
        let headers = quux_otelc_config::export_headers()?;
        let trace_headers = if plan.traces.enabled {
            quux_otelc_config::export_signal_headers("OTEL_EXPORTER_OTLP_TRACES_HEADERS")?
        } else {
            BTreeMap::new()
        };
        let (stop, receiver) = mpsc::sync_channel::<mpsc::SyncSender<()>>(1);
        let worker_state = state.clone();
        let worker_plan = plan.clone();
        std::thread::Builder::new()
            .name("otelc-rust-export".into())
            .spawn(move || {
                let mut previous = None;
                loop {
                    let request = receiver
                        .recv_timeout(Duration::from_millis(worker_plan.export.interval_ms));
                    let finishing = match request {
                        Ok(reply) => Some(reply),
                        Err(mpsc::RecvTimeoutError::Timeout) => None,
                        Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    };
                    let revision = worker_state.revision.load(Ordering::Acquire);
                    if previous != Some(revision) {
                        let mut metrics = ResourceMetrics::default();
                        let result = reader
                            .collect(&mut metrics)
                            .map_err(anyhow::Error::from)
                            .and_then(|_| {
                                let payload =
                                    ExportMetricsServiceRequest::from(&metrics).encode_to_vec();
                                let remaining = worker_state
                                    .deadline
                                    .lock()
                                    .ok()
                                    .and_then(|value| *value)
                                    .map(|end| end.saturating_duration_since(Instant::now()))
                                    .unwrap_or(Duration::from_millis(
                                        worker_plan.export.timeout_ms,
                                    ));
                                if remaining.is_zero() {
                                    anyhow::bail!("Rust export deadline elapsed");
                                }
                                quux_otelc_export::send(
                                    &worker_plan.metrics_endpoint,
                                    &headers,
                                    &payload,
                                    remaining
                                        .min(Duration::from_millis(worker_plan.export.timeout_ms)),
                                )
                            });
                        if result.is_ok() {
                            previous = Some(revision);
                        } else {
                            worker_state.export_loss.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                    if let Some(export) = &worker_plan.trace_export {
                        for _ in 0..worker_plan.export.max_queued_batches {
                            let spans = worker_state
                                .data
                                .lock()
                                .ok()
                                .and_then(|mut data| data.traces.pop());
                            let Some(spans) = spans else {
                                break;
                            };
                            if spans.is_empty() {
                                worker_state.revision.fetch_add(1, Ordering::Release);
                                continue;
                            }
                            let payload = opentelemetry_proto::tonic::collector::trace::v1::ExportTraceServiceRequest {
                                resource_spans: opentelemetry_proto::transform::trace::tonic::group_spans_by_resource_and_scope(spans, &trace_resource),
                            }
                            .encode_to_vec();
                            let remaining = worker_state
                                .deadline
                                .lock()
                                .ok()
                                .and_then(|value| *value)
                                .map(|end| end.saturating_duration_since(Instant::now()))
                                .unwrap_or(Duration::from_millis(export.timeout_ms))
                                .min(Duration::from_millis(export.timeout_ms));
                            if payload.len() > 16 * 1024 * 1024
                                || remaining.is_zero()
                                || quux_otelc_export::send_traces(
                                    &export.endpoint,
                                    &trace_headers,
                                    &payload,
                                    remaining,
                                )
                                .is_err()
                            {
                                worker_state.export_loss.fetch_add(1, Ordering::Relaxed);
                                worker_state.revision.fetch_add(1, Ordering::Release);
                            }
                        }
                    }
                    if let Some(reply) = finishing {
                        worker_state.finished.store(true, Ordering::Release);
                        let _ = reply.send(());
                        break;
                    }
                }
            })?;
        let runtime = Arc::new(Self {
            owner,
            plan,
            state,
            provider,
            calls,
            unwinds,
            cancellations,
            duration,
            stop,
            control: Mutex::new(None),
        });
        if let Some(path) = runtime.plan.runtime.control_socket.as_deref() {
            match control::Control::bind(path, runtime.clone()) {
                Ok(control) => {
                    *runtime
                        .control
                        .lock()
                        .map_err(|_| anyhow::anyhow!("Rust control state poisoned"))? =
                        Some(control)
                }
                Err(error) => {
                    runtime.close();
                    return Err(error);
                }
            }
        }
        Ok(runtime)
    }
    pub fn enter(self: &Arc<Self>, name: &str) -> Guard {
        let mut guard = Guard {
            runtime: None,
            token: 0,
            context: None,
        };
        if self.state.closed.load(Ordering::Acquire) {
            return guard;
        }
        let metrics = self.state.enabled.load(Ordering::Acquire);
        if !metrics && !self.plan.traces.enabled {
            return guard;
        }
        if let Ok(mut data) = self.state.data.lock() {
            if self.state.closed.load(Ordering::Acquire) {
                return guard;
            }
            let metrics = metrics && self.state.enabled.load(Ordering::Acquire);
            if !metrics && !self.plan.traces.enabled {
                return guard;
            }
            let parent = traces::current(self.owner);
            let reason = if name.len() > 1024
                || !data.functions.contains_key(name)
                    && data.functions.len() >= self.plan.runtime.max_functions
            {
                Some("function_capacity")
            } else if data.pending.len() >= self.plan.runtime.max_active_calls {
                Some("active_call_capacity")
            } else if data.next == u64::MAX {
                Some("invalid")
            } else {
                None
            };
            if let Some(reason) = reason {
                self.lose(&mut data, reason);
                if self.plan.traces.enabled {
                    guard.context = Some(data.traces.reject(self.owner, parent, reason));
                }
                return guard;
            }
            data.functions.entry(name.into()).or_insert_with(|| {
                let name = Arc::<str>::from(name);
                Function {
                    name: name.clone(),
                    count: 0,
                    unwinds: 0,
                    cancellations: 0,
                    attributes: vec![KeyValue::new("code.function.name", name)],
                }
            });
            let started = Instant::now();
            let trace_name = data.functions[name].name.clone();
            let context = self.plan.traces.enabled.then(|| {
                data.traces
                    .enter(self.owner, parent, trace_name, started, &self.plan.traces)
            });
            guard.context = context;
            if !metrics && context.is_some_and(|context| !context.sampled) {
                return guard;
            }
            data.next += 1;
            guard.token = data.next;
            guard.context = context;
            guard.runtime = Some(self.clone());
            data.pending.insert(
                guard.token,
                Frame {
                    name: name.into(),
                    started,
                    was_panicking: std::thread::panicking(),
                    metrics,
                    trace: context,
                },
            );
        }
        guard
    }
    pub fn enter_scoped(self: &Arc<Self>, name: &str) -> SyncGuard {
        let guard = self.enter(name);
        let scope = self
            .plan
            .traces
            .enabled
            .then(|| traces::Scope::attach(guard.context));
        SyncGuard { scope, guard }
    }
    fn lose(&self, data: &mut Data, reason: &'static str) {
        if let Some(count) = data.losses.get_mut(reason) {
            *count = count.saturating_add(1);
            self.state.revision.fetch_add(1, Ordering::Release);
        }
    }
    fn finish(&self, token: u64, cancelled: bool) {
        let ended = Instant::now();
        if let Ok(mut data) = self.state.data.lock() {
            let Some(frame) = data.pending.remove(&token) else {
                return;
            };
            let Some(function) = data.functions.get_mut(&frame.name) else {
                return;
            };
            let escaped = std::thread::panicking() && !frame.was_panicking;
            let cancelled = cancelled && !escaped;
            if frame.metrics {
                function.count = function.count.saturating_add(1);
                if escaped {
                    function.unwinds = function.unwinds.saturating_add(1);
                }
                if cancelled {
                    function.cancellations = function.cancellations.saturating_add(1);
                }
                // Serialize recording with close: completed reports and the final
                // SDK snapshot must agree even when another thread is shutting down.
                self.calls.add(1, &function.attributes);
                self.duration.record(
                    ended.duration_since(frame.started).as_secs_f64(),
                    &function.attributes,
                );
                if escaped {
                    self.unwinds.add(1, &function.attributes);
                }
                if cancelled {
                    self.cancellations.add(1, &function.attributes);
                }
            }
            if let Some(context) = frame.trace {
                data.traces.finish(
                    context,
                    ended,
                    escaped,
                    cancelled,
                    self.plan.export.max_queued_batches,
                );
            }
            self.state.revision.fetch_add(1, Ordering::Release);
        }
    }
    pub fn report(&self) -> serde_json::Value {
        let mut values = BTreeMap::new();
        let mut losses = BTreeMap::new();
        let mut count = 0;
        let mut traces = serde_json::Value::Null;
        if let Ok(data) = self.state.data.lock() {
            for (name, function) in &data.functions {
                count += function.count;
                values.insert(
                    name.clone(),
                    serde_json::json!({"count":function.count,"unwinds":function.unwinds,"cancellations":function.cancellations}),
                );
            }
            traces = data.traces.report();
            losses = data.losses.clone();
            *losses.entry("incomplete").or_default() += data.pending.len() as u64;
        }
        serde_json::json!({"schema_version":1,"traces":traces,"language":"rust","pid":std::process::id(),"function_calls":count,"functions":values,"losses":losses,"export_loss":self.state.export_loss.load(Ordering::Relaxed),"export_finished":self.state.finished.load(Ordering::Acquire)})
    }
    pub fn close(&self) {
        if self.state.closed.swap(true, Ordering::AcqRel) {
            return;
        }
        let end = Instant::now() + Duration::from_millis(self.plan.runtime.shutdown_timeout_ms);
        if let Ok(mut deadline) = self.state.deadline.lock() {
            *deadline = Some(end);
        }
        if let Ok(mut control) = self.control.lock() {
            if let Some(control) = control.take() {
                control.close();
            }
        }
        if let Ok(mut data) = self.state.data.lock() {
            let pending = data.pending.len() as u64;
            *data.losses.entry("incomplete").or_default() += pending;
            data.pending.clear();
            data.traces.shutdown();
        }
        self.state.revision.fetch_add(1, Ordering::Release);
        let (tx, rx) = mpsc::sync_channel(1);
        if self.stop.try_send(tx).is_err()
            || rx
                .recv_timeout(end.saturating_duration_since(Instant::now()))
                .is_err()
        {
            self.state.export_loss.fetch_add(1, Ordering::Relaxed);
        }
        let _ = self.provider.shutdown();
        if let Some(path) = std::env::var_os("OTELC_REPORT_PATH") {
            if let Ok(data) = serde_json::to_vec_pretty(&self.report()) {
                let _ = std::fs::write(path, data);
            }
        }
    }
}
pub struct Guard {
    context: Option<traces::Context>,
    runtime: Option<Arc<Runtime>>,
    token: u64,
}
impl Guard {
    fn finish(&mut self, cancelled: bool) {
        if let Some(runtime) = self.runtime.take() {
            if catch_unwind(AssertUnwindSafe(|| runtime.finish(self.token, cancelled))).is_err() {
                if let Ok(mut data) = runtime.state.data.lock() {
                    runtime.lose(&mut data, "invalid");
                }
            }
        }
    }
}
impl Drop for Guard {
    fn drop(&mut self) {
        self.finish(false);
    }
}
struct AsyncGuard {
    guard: Guard,
    completed: bool,
}
impl Drop for AsyncGuard {
    fn drop(&mut self) {
        self.guard.finish(!self.completed);
    }
}
/// Observe one async body from its first poll through completion or drop.
/// Awaiting the original future adds no scheduling or executor requirement.
pub async fn observe_future<F: std::future::Future>(name: &str, future: F) -> F::Output {
    let guard = ACTIVE
        .get()
        .map(|runtime| runtime.enter(name))
        .unwrap_or(Guard {
            runtime: None,
            token: 0,
            context: None,
        });
    observe_with_guard(guard, future).await
}
async fn observe_with_guard<F: std::future::Future>(guard: Guard, future: F) -> F::Output {
    let mut guard = AsyncGuard {
        guard,
        completed: false,
    };
    let output = traces::InContext {
        future: Some(future),
        context: guard.guard.context,
    }
    .await;
    guard.completed = true;
    output
}
pub struct Shutdown;
impl Drop for Shutdown {
    fn drop(&mut self) {
        if let Some(runtime) = ACTIVE.get() {
            let _ = catch_unwind(AssertUnwindSafe(|| runtime.close()));
        }
    }
}
pub fn launch() -> Shutdown {
    if ACTIVE.get().is_none() {
        if let Some(path) = std::env::var_os("OTELC_RUST_PLAN") {
            match policy::Plan::load(std::path::Path::new(&path)).and_then(Runtime::new) {
                Ok(runtime) => {
                    let _ = ACTIVE.set(runtime);
                    std::env::remove_var("OTELC_RUST_PLAN");
                }
                Err(error) => {
                    eprintln!("otelc Rust: {error}");
                    std::process::exit(2);
                }
            }
        }
    }
    Shutdown
}
/// Synchronous guards restore thread context before recording completion.
pub struct SyncGuard {
    scope: Option<traces::Scope>,
    guard: Guard,
}
impl Drop for SyncGuard {
    fn drop(&mut self) {
        self.scope.take();
        self.guard.finish(false);
    }
}
pub fn enter(name: &str) -> SyncGuard {
    if let Some(runtime) = ACTIVE.get() {
        runtime.enter_scoped(name)
    } else {
        SyncGuard {
            scope: None,
            guard: Guard {
                runtime: None,
                token: 0,
                context: None,
            },
        }
    }
}
