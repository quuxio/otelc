//! Source-free Rust body guards and language-native SDK metrics.
mod control;
pub mod policy;
mod reader;
#[cfg(test)]
mod tests;
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
    count: u64,
    unwinds: u64,
    attributes: Vec<KeyValue>,
}
struct Frame {
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
    plan: policy::Plan,
    state: Arc<State>,
    provider: SdkMeterProvider,
    calls: Counter<u64>,
    unwinds: Counter<u64>,
    duration: Histogram<f64>,
    stop: mpsc::SyncSender<mpsc::SyncSender<()>>,
    control: Mutex<Option<control::Control>>,
}
static ACTIVE: OnceLock<Arc<Runtime>> = OnceLock::new();
impl Runtime {
    pub fn new(plan: policy::Plan) -> Result<Arc<Self>> {
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
        let maximum = plan.runtime.max_functions + 1;
        let provider = SdkMeterProvider::builder()
            .with_reader(reader.clone())
            .with_resource(
                Resource::builder_empty()
                    .with_attributes(attributes)
                    .build(),
            )
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
        let headers = quux_otelc_config::export_headers()?;
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
                    if let Some(reply) = finishing {
                        worker_state.finished.store(true, Ordering::Release);
                        let _ = reply.send(());
                        break;
                    }
                }
            })?;
        let runtime = Arc::new(Self {
            plan,
            state,
            provider,
            calls,
            unwinds,
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
        let mut token = 0;
        if self.state.enabled.load(Ordering::Acquire) && !self.state.closed.load(Ordering::Acquire)
        {
            if let Ok(mut data) = self.state.data.lock() {
                if !self.state.enabled.load(Ordering::Acquire)
                    || self.state.closed.load(Ordering::Acquire)
                {
                    return Guard {
                        runtime: None,
                        token: 0,
                    };
                }
                if !data.functions.contains_key(name) {
                    if data.functions.len() >= self.plan.runtime.max_functions || name.len() > 1024
                    {
                        self.lose(&mut data, "function_capacity");
                    } else {
                        data.functions.insert(
                            name.into(),
                            Function {
                                count: 0,
                                unwinds: 0,
                                attributes: vec![KeyValue::new(
                                    "code.function.name",
                                    name.to_owned(),
                                )],
                            },
                        );
                    }
                }
                if data.functions.contains_key(name) {
                    if data.pending.len() >= self.plan.runtime.max_active_calls {
                        self.lose(&mut data, "active_call_capacity");
                    } else if let Some(next) = data.next.checked_add(1) {
                        data.next = next;
                        token = next;
                        data.pending.insert(
                            next,
                            Frame {
                                name: name.into(),
                                started: Instant::now(),
                                was_panicking: std::thread::panicking(),
                            },
                        );
                    } else {
                        self.lose(&mut data, "invalid");
                    }
                }
            }
        }
        Guard {
            runtime: if token == 0 { None } else { Some(self.clone()) },
            token,
        }
    }
    fn lose(&self, data: &mut Data, reason: &'static str) {
        if let Some(count) = data.losses.get_mut(reason) {
            *count = count.saturating_add(1);
            self.state.revision.fetch_add(1, Ordering::Release);
        }
    }
    fn finish(&self, token: u64) {
        let ended = Instant::now();
        if let Ok(mut data) = self.state.data.lock() {
            let Some(frame) = data.pending.remove(&token) else {
                return;
            };
            let Some(function) = data.functions.get_mut(&frame.name) else {
                return;
            };
            let escaped = std::thread::panicking() && !frame.was_panicking;
            function.count = function.count.saturating_add(1);
            if escaped {
                function.unwinds = function.unwinds.saturating_add(1);
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
            self.state.revision.fetch_add(1, Ordering::Release);
        }
    }
    pub fn report(&self) -> serde_json::Value {
        let mut values = BTreeMap::new();
        let mut losses = BTreeMap::new();
        let mut count = 0;
        if let Ok(data) = self.state.data.lock() {
            for (name, function) in &data.functions {
                count += function.count;
                values.insert(
                    name.clone(),
                    serde_json::json!({"count":function.count,"unwinds":function.unwinds}),
                );
            }
            losses = data.losses.clone();
            *losses.entry("incomplete").or_default() += data.pending.len() as u64;
        }
        serde_json::json!({"schema_version":1,"language":"rust","pid":std::process::id(),"function_calls":count,"functions":values,"losses":losses,"export_loss":self.state.export_loss.load(Ordering::Relaxed),"export_finished":self.state.finished.load(Ordering::Acquire)})
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
    runtime: Option<Arc<Runtime>>,
    token: u64,
}
impl Drop for Guard {
    fn drop(&mut self) {
        if let Some(runtime) = self.runtime.take() {
            if catch_unwind(AssertUnwindSafe(|| runtime.finish(self.token))).is_err() {
                if let Ok(mut data) = runtime.state.data.lock() {
                    runtime.lose(&mut data, "invalid");
                }
            }
        }
    }
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
pub fn enter(name: &str) -> Guard {
    if let Some(runtime) = ACTIVE.get() {
        runtime.enter(name)
    } else {
        Guard {
            runtime: None,
            token: 0,
        }
    }
}
