//! One producer and one consumer per ring; ownership is enforced by Slot.
#[cfg(feature = "loom-tests")]
use loom::{
    cell::UnsafeCell,
    sync::atomic::{AtomicUsize, Ordering},
};
#[cfg(not(feature = "loom-tests"))]
use std::{
    cell::UnsafeCell,
    sync::atomic::{AtomicUsize, Ordering},
};
#[derive(Clone, Copy, Default)]
#[repr(C)]
pub struct Record {
    pub function_key: u64,
    pub invocation: u64,
    pub parent_invocation: u64,
    pub trace_root: u64,
    pub start_tick: u64,
    pub duration_tick: u64,
    pub thread_slot: u32,
    pub config_generation: u32,
    pub flags: u32,
    pub reserved: u32,
}
#[repr(align(64))]
struct Cursor(AtomicUsize);
pub struct Queue {
    records: Box<[UnsafeCell<Record>]>,
    read: Cursor,
    write: Cursor,
}
// Only the admitted owner writes records, and the worker is the sole consumer.
unsafe impl Sync for Queue {}
impl Queue {
    pub fn new(capacity: usize) -> Self {
        Self {
            records: (0..capacity)
                .map(|_| UnsafeCell::new(Record::default()))
                .collect(),
            read: Cursor(AtomicUsize::new(0)),
            write: Cursor(AtomicUsize::new(0)),
        }
    }
    pub fn push(&self, record: Record) -> bool {
        let write = self.write.0.load(Ordering::Relaxed);
        if write.wrapping_sub(self.read.0.load(Ordering::Acquire)) >= self.records.len() {
            return false;
        }
        // Acquire observed that the consumer released this slot.
        let cell = &self.records[write & (self.records.len() - 1)];
        #[cfg(feature = "loom-tests")]
        cell.with_mut(|pointer| unsafe { *pointer = record });
        #[cfg(not(feature = "loom-tests"))]
        unsafe {
            *cell.get() = record;
        }

        self.write.0.store(write.wrapping_add(1), Ordering::Release);
        true
    }
    pub fn pop(&self) -> Option<Record> {
        let read = self.read.0.load(Ordering::Relaxed);
        if read == self.write.0.load(Ordering::Acquire) {
            return None;
        }
        // The release-published write cursor makes this record initialized.
        let cell = &self.records[read & (self.records.len() - 1)];
        #[cfg(feature = "loom-tests")]
        let record = cell.with(|pointer| unsafe { *pointer });
        #[cfg(not(feature = "loom-tests"))]
        let record = unsafe { *cell.get() };
        self.read.0.store(read.wrapping_add(1), Ordering::Release);
        Some(record)
    }
    pub fn empty(&self) -> bool {
        self.read.0.load(Ordering::Acquire) == self.write.0.load(Ordering::Acquire)
    }
}
#[cfg(all(test, not(feature = "loom-tests")))]
#[path = "tests/queue.rs"]
mod tests;

#[cfg(all(test, feature = "loom-tests"))]
#[path = "tests/model.rs"]
mod model;
