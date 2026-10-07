//! A token is the atomic lease itself, so slot reuse cannot alias a live ID.
use crate::*;
#[repr(align(64))]
struct Object {
    lease: AtomicU64,
    start: AtomicU64,
    key: AtomicUsize,
}
pub(crate) struct Registry {
    pub names: Vec<String>,
    slots: Box<[Object]>,
    next: AtomicU64,
}
impl Registry {
    pub fn new(config: &quux_otelc_config::Objects) -> Self {
        let mut names = config.classes.clone();
        names.sort();
        Self {
            slots: (0..if names.is_empty() { 0 } else { config.max_live })
                .map(|_| Object {
                    lease: AtomicU64::new(0),
                    start: AtomicU64::new(0),
                    key: AtomicUsize::new(0),
                })
                .collect(),
            names,
            next: AtomicU64::new(2),
        }
    }
    pub fn begin(&self, name: &[u8], now: u64) -> Option<u64> {
        let key = self
            .names
            .binary_search_by(|candidate| candidate.as_bytes().cmp(name))
            .ok()?;
        let token = self
            .next
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |n| n.checked_add(1))
            .ok()?;
        for object in &self.slots {
            if object
                .lease
                .compare_exchange(0, 1, Ordering::Acquire, Ordering::Relaxed)
                .is_ok()
            {
                object.key.store(key, Ordering::Relaxed);
                object.start.store(now, Ordering::Relaxed);
                object.lease.store(token, Ordering::Release);
                return Some(token);
            }
        }
        None
    }
    pub fn end(&self, token: u64, now: u64) -> Option<Record> {
        if token < 2 {
            return None;
        }
        for object in &self.slots {
            if object
                .lease
                .compare_exchange(token, 1, Ordering::AcqRel, Ordering::Relaxed)
                .is_ok()
            {
                let record = Record {
                    function_key: object.key.load(Ordering::Relaxed) as u64,
                    invocation: token,
                    start_tick: object.start.load(Ordering::Relaxed),
                    duration_tick: now.saturating_sub(object.start.load(Ordering::Relaxed)),
                    flags: 2,
                    ..Default::default()
                };
                object.lease.store(0, Ordering::Release);
                return Some(record);
            }
        }
        None
    }
    pub fn active(&self) -> usize {
        self.slots
            .iter()
            .filter(|slot| slot.lease.load(Ordering::Acquire) > 1)
            .count()
    }
}
// The bounded C name is consumed immediately and is never retained or exported
// as a pointer. A caller must supply readable, NUL-terminated storage.
#[no_mangle]
pub unsafe extern "C" fn otelc_record_object_begin(name: *const libc::c_char) -> u64 {
    let Some(writer) = writer() else { return 0 };
    if name.is_null() {
        return 0;
    }
    let length = libc::strnlen(name, 256);
    if length == 256 {
        return 0;
    }
    let bytes = std::slice::from_raw_parts(name.cast::<u8>(), length);
    let state = writer.0;
    if !state.metrics_enabled.load(Ordering::Acquire) {
        return 0;
    }
    if state
        .objects
        .names
        .binary_search_by(|candidate| candidate.as_bytes().cmp(bytes))
        .is_err()
    {
        return 0;
    }
    let token = state.objects.begin(bytes, monotonic()).unwrap_or(0);
    if token == 0 {
        increment(&state.object_capacity);
    }
    token
}
#[no_mangle]
pub extern "C" fn otelc_record_object_end(index: isize, token: u64) {
    if token == 0 {
        return;
    }
    let Some(writer) = writer() else { return };
    let state = writer.0;
    let Some(record) = state.objects.end(token, monotonic()) else {
        increment(&state.invalid);
        return;
    };
    if let Some(slot) = state.slots.get(index as usize) {
        if !slot.queue.push(record) {
            increment(&state.queue_loss);
        }
    } else {
        increment(&state.admission);
    }
}
#[cfg(test)]
#[path = "tests/objects.rs"]
mod tests;
