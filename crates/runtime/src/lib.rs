//! Callback runtime. Native TLS owns producer state; workers own aggregation/export.
mod control;
mod objects;
mod queue;
mod traces;
use anyhow::{Context, Result};
use queue::{Queue, Record};
use quux_otelc_config::{export_headers, Config};
use quux_otelc_export::Aggregate;
use quux_otelc_symbols::{manifest_path, Manifest};
use std::{
    cell::UnsafeCell,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, AtomicU8, AtomicUsize, Ordering},
        mpsc, OnceLock,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Copy, Default)]
#[repr(C)]
struct Frame {
    address: u64,
    key: u64,
    start: u64,
    token: u64,
    padding: [u64; 4],
}
struct Producer {
    frames: Box<[Frame]>,
    depth: usize,
    suppressed: usize,
    next_token: u64,
}
struct Slot {
    state: AtomicU8,
    producer: UnsafeCell<Producer>,
    queue: Queue,
}
// Producer mutation belongs to a single admitted native thread. The worker
// touches a retired producer only after state=2 has release-published retirement.
unsafe impl Sync for Slot {}
struct State {
    config: Config,
    functions: Vec<(usize, String)>,
    objects: objects::Registry,
    object_capacity: AtomicU64,
    report_path: Option<PathBuf>,
    summary: OnceLock<(u64, u64)>,
    slots: Box<[Slot]>,
    running: AtomicBool,
    metrics_enabled: AtomicBool,
    completed: AtomicU64,
    control_thread: std::sync::Mutex<Option<control::Thread>>,
    writers: AtomicUsize,
    active_calls: AtomicUsize,
    active_capacity: AtomicU64,
    admission: AtomicU64,
    stack: AtomicU64,
    queue_loss: AtomicU64,
    invalid: AtomicU64,
    incomplete: AtomicU64,
    export_loss: AtomicU64,
    start: u64,
    done: std::sync::Mutex<Option<mpsc::Receiver<()>>>,
    deadline: std::sync::Mutex<Option<Instant>>,
}
static STATE: OnceLock<State> = OnceLock::new();
extern "C" {
    fn otelc_image_slide() -> isize;
}
fn increment(counter: &AtomicU64) {
    let _ = counter.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |v| {
        Some(v.saturating_add(1))
    });
}
fn realtime() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .min(u64::MAX as u128) as u64
}
fn monotonic() -> u64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    unsafe {
        libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts);
    }
    (ts.tv_sec as u64)
        .saturating_mul(1000000000)
        .saturating_add(ts.tv_nsec as u64)
}
fn initialize() -> Result<()> {
    let Some(config_path) = std::env::var_os("OTELC_CONFIG") else {
        return Ok(());
    };
    let config = Config::load(&PathBuf::from(config_path), true)?;
    let binary = std::env::current_exe()?;
    let path = std::env::var_os("OTELC_MANIFEST")
        .map(PathBuf::from)
        .unwrap_or_else(|| manifest_path(&binary));
    let manifest = Manifest::load(&path)?;
    manifest.verify(&binary)?;
    let headers = export_headers()?;
    let selection = config.function_selection()?;
    let slide = unsafe { otelc_image_slide() };
    let functions: Vec<_> = manifest
        .functions
        .iter()
        .filter(|f| {
            f.selected
                && selection.accepts_with_annotation(
                    &f.display_name,
                    config.read_annotations && f.annotated,
                )
        })
        .map(|f| {
            let address = (f.address as i128) + (slide as i128);
            if address <= 0 || address > usize::MAX as i128 {
                anyhow::bail!("invalid relocated function address");
            }
            Ok((address as usize, f.display_name.clone()))
        })
        .collect::<Result<_>>()?;
    if functions.len() > config.runtime.max_functions {
        anyhow::bail!("selected function count exceeds runtime limit");
    }
    let slots = (0..config.runtime.max_threads)
        .map(|_| Slot {
            state: AtomicU8::new(0),
            producer: UnsafeCell::new(Producer {
                frames: vec![Frame::default(); config.runtime.stack_depth].into_boxed_slice(),
                depth: 0,
                suppressed: 0,
                next_token: 1,
            }),
            queue: Queue::new(config.runtime.queue_capacity),
        })
        .collect();
    let control = config
        .runtime
        .control_socket
        .as_deref()
        .map(control::bind)
        .transpose()?;
    let (done_tx, done_rx) = mpsc::channel();
    let objects = objects::Registry::new(&config.objects);
    STATE
        .set(State {
            metrics_enabled: AtomicBool::new(config.metrics.enabled),
            completed: AtomicU64::new(0),
            control_thread: std::sync::Mutex::new(None),
            config,
            functions,
            objects,
            object_capacity: AtomicU64::new(0),
            report_path: std::env::var_os("OTELC_REPORT_PATH").map(PathBuf::from),
            summary: OnceLock::new(),
            slots,
            running: AtomicBool::new(false),
            writers: AtomicUsize::new(0),
            active_calls: AtomicUsize::new(0),
            active_capacity: AtomicU64::new(0),
            admission: AtomicU64::new(0),
            stack: AtomicU64::new(0),
            queue_loss: AtomicU64::new(0),
            invalid: AtomicU64::new(0),
            incomplete: AtomicU64::new(0),
            export_loss: AtomicU64::new(0),
            start: realtime(),
            done: std::sync::Mutex::new(Some(done_rx)),
            deadline: std::sync::Mutex::new(None),
        })
        .map_err(|_| anyhow::anyhow!("runtime already initialized"))?;
    let state = STATE.get().context("runtime unavailable")?;
    let (tx, rx) = mpsc::sync_channel::<Vec<u8>>(state.config.export.max_queued_batches);
    let exporter = std::thread::Builder::new()
        .name("otelc-export".into())
        .spawn(move || {
            let endpoint = state.config.metrics_endpoint();
            while let Ok(batch) = rx.recv() {
                let remaining = state
                    .deadline
                    .lock()
                    .ok()
                    .and_then(|d| *d)
                    .map(|d| d.saturating_duration_since(Instant::now()))
                    .unwrap_or(Duration::from_millis(state.config.export.timeout_ms));
                if remaining.is_zero()
                    || quux_otelc_export::send(
                        &endpoint,
                        &headers,
                        &batch,
                        remaining.min(Duration::from_millis(state.config.export.timeout_ms)),
                    )
                    .is_err()
                {
                    increment(&state.export_loss);
                }
            }
            let _ = done_tx.send(());
        })?;
    state.running.store(true, Ordering::Release);
    if let Err(error) = std::thread::Builder::new()
        .name("otelc-worker".into())
        .spawn(move || {
            worker(state, tx);
            drop(exporter);
        })
    {
        state.running.store(false, Ordering::Release);
        return Err(error.into());
    }
    if let Some(control) = control {
        let (control_tx, control_rx) = mpsc::channel();
        let thread = match std::thread::Builder::new()
            .name("otelc-control".into())
            .spawn(move || {
                control::serve(control, state);
                let _ = control_tx.send(());
            }) {
            Ok(thread) => thread,
            Err(error) => {
                state.running.store(false, Ordering::Release);
                return Err(error.into());
            }
        };
        *state
            .control_thread
            .lock()
            .map_err(|_| anyhow::anyhow!("control thread unavailable"))? = Some(control::Thread {
            handle: thread,
            done: control_rx,
        });
    }
    Ok(())
}
fn worker(state: &'static State, tx: mpsc::SyncSender<Vec<u8>>) {
    let mut aggregates: Vec<_> = state
        .functions
        .iter()
        .map(|(_, name)| {
            Aggregate::new(
                name.clone(),
                state.config.metrics.histogram_boundaries_seconds.len(),
            )
        })
        .collect();
    let mut object_aggregates: Vec<_> = state
        .objects
        .names
        .iter()
        .map(|name| {
            Aggregate::new(
                name.clone(),
                state.config.metrics.histogram_boundaries_seconds.len(),
            )
        })
        .collect();
    let instance = format!("{}-{}", std::process::id(), state.start);
    let mut last = Instant::now();
    let mut dirty = false;
    loop {
        for slot in &state.slots {
            // Bound each turn so one busy producer cannot starve other slots.
            for _ in 0..state.config.runtime.queue_capacity {
                let Some(record) = slot.queue.pop() else {
                    break;
                };
                dirty = true;
                if record.flags & 2 == 0 {
                    increment(&state.completed);
                }
                let collection = if record.flags & 2 != 0 {
                    &mut object_aggregates
                } else {
                    &mut aggregates
                };
                if let Some(aggregate) = collection.get_mut(record.function_key as usize) {
                    if record.flags & 1 != 0 {
                        aggregate.unwinds = aggregate.unwinds.saturating_add(1);
                    }
                    aggregate.observe(
                        record.duration_tick as f64 / 1e9,
                        &state.config.metrics.histogram_boundaries_seconds,
                    );
                }
            }
            if slot.state.load(Ordering::Acquire) == 2 && slot.queue.empty() {
                let producer = unsafe { &mut *slot.producer.get() };
                for _ in 0..producer.depth {
                    increment(&state.incomplete);
                }
                state
                    .active_calls
                    .fetch_sub(producer.depth, Ordering::AcqRel);
                producer.depth = 0;
                producer.suppressed = 0;
                slot.state.store(0, Ordering::Release);
            }
        }
        let stopping = !state.running.load(Ordering::Acquire)
            && state.writers.load(Ordering::Acquire) == 0
            && state.slots.iter().all(|s| s.queue.empty());
        if stopping {
            for slot in &state.slots {
                if slot.state.load(Ordering::Acquire) == 1 {
                    let producer = unsafe { &*slot.producer.get() };
                    for _ in 0..producer.depth {
                        increment(&state.incomplete);
                    }
                }
            }
        }
        if last.elapsed() >= Duration::from_millis(state.config.export.interval_ms) || stopping {
            if state.metrics_enabled.load(Ordering::Acquire) || dirty || stopping {
                let losses = [
                    ("thread_admission", state.admission.load(Ordering::Relaxed)),
                    (
                        "active_call_capacity",
                        state.active_capacity.load(Ordering::Relaxed),
                    ),
                    (
                        "object_capacity",
                        state.object_capacity.load(Ordering::Relaxed),
                    ),
                    ("stack", state.stack.load(Ordering::Relaxed)),
                    ("queue", state.queue_loss.load(Ordering::Relaxed)),
                    ("invalid_exit", state.invalid.load(Ordering::Relaxed)),
                    ("incomplete", state.incomplete.load(Ordering::Relaxed)),
                    (
                        "object_incomplete",
                        if stopping {
                            state.objects.active() as u64
                        } else {
                            0
                        },
                    ),
                ];
                let batch = quux_otelc_export::encode(
                    &state.config,
                    (&aggregates, &object_aggregates),
                    &losses,
                    state.export_loss.load(Ordering::Relaxed),
                    state.start,
                    realtime(),
                    &instance,
                );
                if tx.try_send(batch).is_err() {
                    increment(&state.export_loss);
                }
            }
            dirty = false;
            last = Instant::now();
        }
        if stopping {
            let _ = state.summary.set((
                aggregates
                    .iter()
                    .fold(0u64, |n, a| n.saturating_add(a.count)),
                object_aggregates
                    .iter()
                    .fold(0u64, |n, a| n.saturating_add(a.count)),
            ));
            break;
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}
#[no_mangle]
pub extern "C" fn otelc_initialize() {
    if std::panic::catch_unwind(initialize)
        .ok()
        .and_then(Result::err)
        .is_some()
    {
        eprintln!(
            "otelc: initialization failed; telemetry disabled (check configuration and manifest)"
        );
    }
}
#[no_mangle]
pub extern "C" fn otelc_shutdown() {
    let Some(state) = STATE.get() else { return };
    let deadline = Instant::now() + Duration::from_millis(state.config.runtime.shutdown_timeout_ms);
    if let Ok(mut value) = state.deadline.lock() {
        *value = Some(deadline);
    }
    state.running.store(false, Ordering::Release);
    while state.writers.load(Ordering::Acquire) != 0 {
        if Instant::now() >= deadline {
            return;
        }
        std::thread::yield_now();
    }
    if let Ok(mut thread) = state.control_thread.lock() {
        if let Some(thread) = thread.take() {
            if thread
                .done
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .is_ok()
            {
                let _ = thread.handle.join();
            }
        }
    }
    let mut export_finished = false;
    if let Ok(mut done) = state.done.lock() {
        if let Some(rx) = done.take() {
            export_finished = rx
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .is_ok();
        }
    }
    if let Some(path) = &state.report_path {
        let summary = state.summary.get();
        let report = serde_json::json!({"drained": summary.is_some(), "export_finished": export_finished, "function_calls": summary.map(|s|s.0), "object_lifetimes":summary.map(|s|s.1), "losses": { "thread_admission":state.admission.load(Ordering::Relaxed), "active_call_capacity":state.active_capacity.load(Ordering::Relaxed), "stack":state.stack.load(Ordering::Relaxed),"queue":state.queue_loss.load(Ordering::Relaxed),"invalid_exit":state.invalid.load(Ordering::Relaxed),"incomplete":state.incomplete.load(Ordering::Relaxed),"object_capacity":state.object_capacity.load(Ordering::Relaxed),"object_incomplete":state.objects.active()},"export_dropped_batches":state.export_loss.load(Ordering::Relaxed)});
        if serde_json::to_vec(&report)
            .ok()
            .and_then(|bytes| std::fs::write(path, bytes).ok())
            .is_none()
        {
            eprintln!("otelc: could not write requested observation report");
        }
    }
}
#[no_mangle]
pub extern "C" fn otelc_register_thread() -> isize {
    let Some(state) = STATE.get().filter(|s| s.running.load(Ordering::Acquire)) else {
        return -1;
    };
    for (index, slot) in state.slots.iter().enumerate() {
        if slot
            .state
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Relaxed)
            .is_ok()
        {
            return index as isize;
        }
    }
    -1
}
#[no_mangle]
pub extern "C" fn otelc_retire_thread(index: isize) {
    if let Some(state) = STATE.get() {
        if let Some(slot) = state.slots.get(index as usize) {
            slot.state.store(2, Ordering::Release);
        }
    }
}
struct Writer<'a>(&'a State);
impl Drop for Writer<'_> {
    fn drop(&mut self) {
        self.0.writers.fetch_sub(1, Ordering::Release);
    }
}
fn writer() -> Option<Writer<'static>> {
    let state = STATE.get()?;
    if !state.running.load(Ordering::Acquire) {
        return None;
    }
    state.writers.fetch_add(1, Ordering::Acquire);
    if !state.running.load(Ordering::Acquire) {
        state.writers.fetch_sub(1, Ordering::Release);
        return None;
    }
    Some(Writer(state))
}
fn reserve_call(count: &AtomicUsize, maximum: usize) -> bool {
    count
        .fetch_update(Ordering::AcqRel, Ordering::Acquire, |value| {
            (value < maximum).then_some(value + 1)
        })
        .is_ok()
}
fn admit_call(state: &State) -> bool {
    if reserve_call(&state.active_calls, state.config.runtime.max_active_calls) {
        true
    } else {
        increment(&state.active_capacity);
        false
    }
}
#[no_mangle]
pub extern "C" fn otelc_record_enter(index: isize, address: usize) {
    let Some(writer) = writer() else { return };
    let state = writer.0;
    if !state.metrics_enabled.load(Ordering::Acquire) {
        return;
    }
    let Some(slot) = state.slots.get(index as usize) else {
        return;
    };
    let Ok(key) = state.functions.binary_search_by_key(&address, |f| f.0) else {
        return;
    };
    let producer = unsafe { &mut *slot.producer.get() };
    if producer.depth == producer.frames.len() || producer.suppressed > 0 {
        producer.suppressed = producer.suppressed.saturating_add(1);
        increment(&state.stack);
        return;
    }
    if !admit_call(state) {
        producer.suppressed = 1;
        return;
    }
    producer.frames[producer.depth] = Frame {
        address: address as u64,
        key: key as u64,
        start: monotonic(),
        token: 0,
        padding: [0; 4],
    };
    producer.depth += 1;
}
#[no_mangle]
pub extern "C" fn otelc_record_exit(index: isize, address: usize) {
    if STATE
        .get()
        .is_some_and(|s| !s.metrics_enabled.load(Ordering::Acquire))
    {
        return;
    }
    let Some(writer) = writer() else { return };
    let state = writer.0;
    let Some(slot) = state.slots.get(index as usize) else {
        return;
    };
    if state
        .functions
        .binary_search_by_key(&address, |f| f.0)
        .is_err()
    {
        return;
    }
    let producer = unsafe { &mut *slot.producer.get() };
    if producer.suppressed > 0 {
        producer.suppressed -= 1;
        return;
    }
    if producer.depth == 0 {
        increment(&state.invalid);
        return;
    }
    let frame = producer.frames[producer.depth - 1];
    producer.depth -= 1;
    state.active_calls.fetch_sub(1, Ordering::AcqRel);
    if frame.address != address as u64 {
        state
            .active_calls
            .fetch_sub(producer.depth, Ordering::AcqRel);
        producer.depth = 0;
        increment(&state.invalid);
        return;
    }
    let Some(duration) = monotonic().checked_sub(frame.start) else {
        increment(&state.invalid);
        return;
    };
    if !slot.queue.push(Record {
        function_key: frame.key,
        start_tick: frame.start,
        duration_tick: duration,
        thread_slot: index as u32,
        ..Default::default()
    }) {
        increment(&state.queue_loss);
    }
}
#[cfg(test)]
#[path = "tests/runtime.rs"]
mod tests;

#[no_mangle]
pub extern "C" fn otelc_record_denied(address: usize) {
    if let Some(state) = STATE.get().filter(|s| {
        s.running.load(Ordering::Acquire)
            && (s.metrics_enabled.load(Ordering::Acquire) || s.config.traces.enabled)
    }) {
        if state
            .functions
            .binary_search_by_key(&address, |f| f.0)
            .is_ok()
        {
            increment(&state.admission);
        }
    }
}

// The compiler keeps this token in the invocation's SSA value. No token or
// exception can cross threads or the FFI boundary; zero is always a no-op.
#[no_mangle]
pub extern "C" fn otelc_record_token_enter(index: isize, address: usize) -> u64 {
    if !STATE
        .get()
        .is_some_and(|s| s.metrics_enabled.load(Ordering::Acquire) || s.config.traces.enabled)
    {
        return 0;
    }
    let Some(writer) = writer() else { return 0 };
    let state = writer.0;
    let Some(slot) = state.slots.get(index as usize) else {
        return 0;
    };
    let Ok(key) = state.functions.binary_search_by_key(&address, |f| f.0) else {
        return 0;
    };
    let producer = unsafe { &mut *slot.producer.get() };
    let tracing = state.config.traces.enabled;
    if tracing && producer.suppressed != 0 {
        producer.suppressed = producer.suppressed.saturating_add(1);
        increment(&state.stack);
        return u64::MAX;
    }
    let full = producer.depth == producer.frames.len() || producer.next_token == u64::MAX;
    if full {
        increment(&state.stack);
    }
    if full || !admit_call(state) {
        if tracing {
            if producer.depth != 0 {
                producer.frames[0].padding[2] |= traces::INVALID_ROOT as u64;
            }
            producer.suppressed = 1;
            return u64::MAX;
        }
        return 0;
    }
    let token = producer.next_token;
    producer.next_token += 1;
    let parent = if producer.depth == 0 {
        0
    } else {
        producer.frames[producer.depth - 1].token
    };
    let root = if producer.depth == 0 {
        token
    } else {
        producer.frames[0].token
    };
    let flags = if state.metrics_enabled.load(Ordering::Acquire) {
        0
    } else {
        traces::METRIC_DISABLED as u64
    };
    let start = monotonic();
    producer.frames[producer.depth] = Frame {
        address: address as u64,
        key: key as u64,
        start,
        token,
        padding: [parent, root, flags, 0],
    };
    producer.depth += 1;
    if tracing {
        producer.frames[0].padding[3] = producer.frames[0].padding[3].saturating_add(1);
        if producer.depth == 1
            && !slot.queue.push(Record {
                function_key: key as u64,
                invocation: token,
                trace_root: token,
                start_tick: start,
                thread_slot: index as u32,
                flags: traces::ROOT_ENTER,
                ..Default::default()
            })
        {
            increment(&state.queue_loss);
            producer.frames[0].padding[2] |= traces::INVALID_ROOT as u64;
        }
    }
    token
}

#[no_mangle]
pub extern "C" fn otelc_record_token_exit(index: isize, token: u64, kind: u32) {
    if token == 0 {
        return;
    }
    let Some(writer) = writer() else { return };
    let state = writer.0;
    let Some(slot) = state.slots.get(index as usize) else {
        return;
    };
    let producer = unsafe { &mut *slot.producer.get() };
    if token == u64::MAX && state.config.traces.enabled {
        if producer.suppressed != usize::MAX && producer.suppressed != 0 {
            producer.suppressed -= 1;
        }
        return;
    }
    if producer.depth == 0 {
        increment(&state.invalid);
        return;
    }
    let frame = producer.frames[producer.depth - 1];
    producer.depth -= 1;
    state.active_calls.fetch_sub(1, Ordering::AcqRel);
    if frame.token != token || kind > 1 {
        state
            .active_calls
            .fetch_sub(producer.depth, Ordering::AcqRel);
        producer.depth = 0;
        increment(&state.invalid);
        return;
    }
    let Some(duration) = monotonic().checked_sub(frame.start) else {
        increment(&state.invalid);
        return;
    };
    if !slot.queue.push(Record {
        function_key: frame.key,
        invocation: token,
        start_tick: frame.start,
        duration_tick: duration,
        thread_slot: index as u32,
        parent_invocation: frame.padding[0],
        trace_root: if state.config.traces.enabled {
            frame.padding[1]
        } else {
            0
        },
        flags: kind
            | frame.padding[2] as u32
            | if state.config.traces.enabled {
                traces::TRACE_EXIT
            } else {
                0
            }
            | if frame.padding[3] > u32::MAX as u64 {
                traces::INVALID_ROOT
            } else {
                0
            },
        reserved: frame.padding[3].min(u32::MAX as u64) as u32,
        ..Default::default()
    }) {
        increment(&state.queue_loss);
        if state.config.traces.enabled && producer.depth != 0 {
            producer.frames[0].padding[2] |= traces::INVALID_ROOT as u64;
        }
    }
}
