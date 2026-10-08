//! Workers construct SDK trees from primitive producer records, never probe code.
use crate::queue::Record;
use quux_otelc_config::Traces;
use quux_otelc_export::traces::{Context, Store};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime},
};

pub(super) const ROOT_ENTER: u32 = 4;
pub(super) const TRACE_EXIT: u32 = 8;
pub(super) const METRIC_DISABLED: u32 = 16;
pub(super) const INVALID_ROOT: u32 = 32;
pub(super) const ROOT_DENIED: u32 = 64;
pub(super) const ROOT_RESERVED: u32 = 128;
const RECORD_CAPACITY: usize = 1_048_576;

pub(super) struct Exporter {
    pub wake: std::sync::mpsc::SyncSender<()>,
    done: std::sync::mpsc::Receiver<()>,
    handle: crate::thread::Handle,
}
impl Exporter {
    pub fn finish(self, deadline: Instant) -> bool {
        let Self { wake, done, handle } = self;
        drop(wake);
        let finished = done
            .recv_timeout(deadline.saturating_duration_since(Instant::now()))
            .is_ok()
            && Instant::now() <= deadline;
        // The acknowledgement completes export. Thread-local destructors must not
        // extend the caller's budget, so the native handle is always detached.
        drop(handle);
        finished
    }
}
pub(super) fn spawn_exporter(state: &'static crate::State) -> anyhow::Result<Option<Exporter>> {
    use opentelemetry::KeyValue;
    use opentelemetry_proto::{
        tonic::collector::trace::v1::ExportTraceServiceRequest,
        transform::{
            common::tonic::ResourceAttributesWithSchema,
            trace::tonic::group_spans_by_resource_and_scope,
        },
    };
    use prost::Message;
    let Some(store) = &state.trace_store else {
        return Ok(None);
    };
    let export = state
        .config
        .trace_export
        .as_ref()
        .expect("validated trace export");
    let headers = quux_otelc_config::export_signal_headers("OTEL_EXPORTER_OTLP_TRACES_HEADERS")?;
    let (wake, pending) = std::sync::mpsc::sync_channel(1);
    let (complete, done) = std::sync::mpsc::channel();
    let handle = crate::thread::spawn(c"otelc-traces", move || {
        let mut attributes = vec![
            KeyValue::new("service.name", state.config.resource.service_name.clone()),
            KeyValue::new(
                "service.version",
                state.config.resource.service_version.clone(),
            ),
            KeyValue::new(
                "service.instance.id",
                format!("{}-{}", std::process::id(), state.start),
            ),
        ];
        attributes.extend(
            state
                .config
                .resource
                .attributes
                .iter()
                .map(|(key, value)| KeyValue::new(key.clone(), value.clone())),
        );
        let resource = opentelemetry_sdk::Resource::builder_empty()
            .with_attributes(attributes)
            .build();
        let resource = ResourceAttributesWithSchema::from(&resource);
        loop {
            let stopping = pending.recv().is_err();
            for _ in 0..state.config.export.max_queued_batches {
                let remaining = state
                    .deadline
                    .lock()
                    .ok()
                    .and_then(|value| *value)
                    .map(|end| end.saturating_duration_since(Instant::now()))
                    .unwrap_or(Duration::from_millis(export.timeout_ms))
                    .min(Duration::from_millis(export.timeout_ms));
                if remaining.is_zero() {
                    store
                        .lock()
                        .expect("trace store poisoned")
                        .discard_ready("shutdown");
                    break;
                }
                let phase = Instant::now() + remaining;
                let spans = store.lock().expect("trace store poisoned").pop();
                let Some(spans) = spans else { break };
                if spans.is_empty() {
                    continue;
                }
                let payload = ExportTraceServiceRequest {
                    resource_spans: group_spans_by_resource_and_scope(spans, &resource),
                }
                .encode_to_vec();
                let remaining = phase.saturating_duration_since(Instant::now()).min(
                    state
                        .deadline
                        .lock()
                        .ok()
                        .and_then(|value| *value)
                        .map(|end| end.saturating_duration_since(Instant::now()))
                        .unwrap_or(Duration::from_millis(export.timeout_ms)),
                );
                if payload.len() > 16 * 1024 * 1024
                    || remaining.is_zero()
                    || quux_otelc_export::send_traces(
                        &export.endpoint,
                        &headers,
                        &payload,
                        remaining,
                    )
                    .is_err()
                {
                    crate::increment(&state.export_loss);
                    store
                        .lock()
                        .expect("trace store poisoned")
                        .reject(1, None, "export");
                }
            }
            if stopping {
                break;
            }
        }
        let _ = complete.send(());
    })?;
    Ok(Some(Exporter { wake, done, handle }))
}

struct Root {
    header: Record,
    context: Context,
    invalid: bool,
    records: Vec<Record>,
}
pub(super) struct Bridge<'a> {
    roots: Box<[Option<Root>]>,
    retained: usize,
    store: &'a Mutex<Store>,
    names: Box<[Arc<str>]>,
    policy: &'a Traces,
    queue_capacity: usize,
    tick: u64,
    origin: Instant,
    epoch: SystemTime,
}
impl<'a> Bridge<'a> {
    pub fn new(
        store: &'a Mutex<Store>,
        policy: &'a Traces,
        names: impl IntoIterator<Item = Arc<str>>,
        limits: (usize, usize),
        clock: (u64, Instant, SystemTime),
    ) -> Self {
        Self {
            roots: (0..limits.0).map(|_| None).collect(),
            retained: 0,
            store,
            names: names.into_iter().collect(),
            policy,
            queue_capacity: limits.1,
            tick: clock.0,
            origin: clock.1,
            epoch: clock.2,
        }
    }
    fn timestamp(&self, tick: u64) -> Option<Instant> {
        self.origin
            .checked_add(Duration::from_nanos(tick.checked_sub(self.tick)?))
    }
    fn epoch(&self, tick: u64) -> Option<SystemTime> {
        self.epoch
            .checked_add(Duration::from_nanos(tick.checked_sub(self.tick)?))
    }
    fn reject(&mut self, root: &mut Root, reason: &'static str) {
        if root.context.sampled && !root.invalid {
            self.store
                .lock()
                .expect("trace store poisoned")
                .reject(1, Some(root.context), reason);
            self.retained -= root.records.len();
            root.records.clear();
            root.invalid = true;
        }
    }
    fn abandon(&mut self, mut root: Root, reason: &'static str) {
        self.reject(&mut root, reason);
        self.store.lock().expect("trace store poisoned").finish(
            root.context,
            self.origin,
            false,
            false,
            self.queue_capacity,
        );
    }
    pub fn retire(&mut self, slot: usize) {
        if let Some(root) = self.roots[slot].take() {
            self.abandon(root, "incomplete");
        }
    }
    pub fn shutdown(&mut self) {
        for slot in 0..self.roots.len() {
            self.retire(slot);
        }
        self.store.lock().expect("trace store poisoned").shutdown();
    }
    pub fn record(&mut self, record: Record) {
        let slot = record.thread_slot as usize;
        if slot >= self.roots.len() {
            return;
        }
        if record.flags & ROOT_ENTER != 0 {
            self.retire(slot);
            let context = if record.flags & ROOT_DENIED != 0 {
                self.store
                    .lock()
                    .expect("trace store poisoned")
                    .reject(1, None, "trace_capacity")
            } else {
                match (
                    self.timestamp(record.start_tick),
                    if record.duration_tick != 0 {
                        SystemTime::UNIX_EPOCH
                            .checked_add(Duration::from_nanos(record.duration_tick))
                    } else {
                        self.epoch(record.start_tick)
                    },
                    self.names.get(record.function_key as usize),
                ) {
                    (Some(start), Some(epoch), Some(name)) => self
                        .store
                        .lock()
                        .expect("trace store poisoned")
                        .enter_at_epoch(1, None, name.clone(), start, epoch, self.policy),
                    _ => self
                        .store
                        .lock()
                        .expect("trace store poisoned")
                        .reject(1, None, "invalid"),
                }
            };
            let mut root = Root {
                header: record,
                context,
                invalid: false,
                records: Vec::new(),
            };
            if record.parent_invocation != 0
                || record.invocation == 0
                || record.invocation != record.trace_root
            {
                self.reject(&mut root, "invalid");
            }
            self.roots[slot] = Some(root);
            return;
        }
        if record.flags & TRACE_EXIT == 0 {
            return;
        }
        let Some(mut root) = self.roots[slot].take() else {
            // A dropped root-entry record must never promote its child to a root.
            let context =
                self.store
                    .lock()
                    .expect("trace store poisoned")
                    .reject(1, None, "record_gap");
            self.roots[slot] = Some(Root {
                header: Record {
                    invocation: record.trace_root,
                    ..Default::default()
                },
                context,
                invalid: true,
                records: Vec::new(),
            });
            self.record(record);
            return;
        };
        if record.trace_root != root.header.invocation {
            self.abandon(root, "record_gap");
            self.record(record);
            return;
        }
        if record.invocation == root.header.invocation {
            if record.flags & INVALID_ROOT != 0 {
                self.reject(&mut root, "producer_loss");
            }
            if record.parent_invocation != 0
                || record.function_key != root.header.function_key
                || record.start_tick != root.header.start_tick
                || record.reserved as usize != root.records.len() + 1
                    && !root.invalid
                    && root.context.sampled
            {
                self.reject(&mut root, "record_gap");
            }
            self.complete(root, record);
        } else {
            if record.invocation == 0
                || record.parent_invocation == 0
                || record.flags & INVALID_ROOT != 0
            {
                self.reject(&mut root, "invalid");
            }
            if root.context.sampled && !root.invalid {
                let sdk_retained = self.store.lock().expect("trace store poisoned").retained();
                if root.records.len() + 1 >= self.policy.max_spans_per_trace
                    || sdk_retained + self.retained >= RECORD_CAPACITY
                {
                    self.reject(&mut root, "span_capacity");
                } else {
                    root.records.push(record);
                    self.retained += 1;
                }
            }
            self.roots[slot] = Some(root);
        }
    }
    fn complete(&mut self, mut root: Root, record: Record) {
        let end_tick = record.start_tick.checked_add(record.duration_tick);
        let Some(end) = end_tick.and_then(|tick| self.timestamp(tick)) else {
            self.abandon(root, "invalid");
            return;
        };
        if !root.context.sampled || root.invalid {
            self.store.lock().expect("trace store poisoned").finish(
                root.context,
                end,
                record.flags & 1 != 0,
                false,
                self.queue_capacity,
            );
            return;
        }
        self.retained -= root.records.len();
        let records = std::mem::take(&mut root.records);
        let mut store = self.store.lock().expect("trace store poisoned");
        let mut contexts = HashMap::from([(
            record.invocation,
            (root.context, record.start_tick, end_tick.unwrap()),
        )]);
        let mut admitted = Vec::new();
        for child in records.iter().rev() {
            let parent = contexts.get(&child.parent_invocation).copied();
            let bounds = child.start_tick.checked_add(child.duration_tick);
            let start = self.timestamp(child.start_tick);
            let finish = bounds.and_then(|tick| self.timestamp(tick));
            let name = self.names.get(child.function_key as usize);
            let valid = parent.is_some_and(|(_, start, end)| {
                child.start_tick >= start && bounds.is_some_and(|tick| tick <= end)
            });
            if !valid
                || start.is_none()
                || finish.is_none()
                || name.is_none()
                || contexts.contains_key(&child.invocation)
            {
                store.reject(1, Some(root.context), "invalid");
                break;
            }
            let context = store.enter(
                1,
                Some(parent.unwrap().0),
                name.unwrap().clone(),
                start.unwrap(),
                self.policy,
            );
            if !context.sampled {
                break;
            }
            contexts.insert(
                child.invocation,
                (context, child.start_tick, bounds.unwrap()),
            );
            admitted.push((context, finish.unwrap(), child.flags & 1 != 0));
        }
        for (context, end, unwind) in admitted.into_iter().rev() {
            store.finish(context, end, unwind, false, self.queue_capacity);
        }
        store.finish(
            root.context,
            end,
            record.flags & 1 != 0,
            false,
            self.queue_capacity,
        );
    }
}

#[cfg(test)]
#[path = "tests/traces.rs"]
mod tests;
