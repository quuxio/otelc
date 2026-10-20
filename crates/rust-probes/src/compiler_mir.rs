//! Typed compiler hooks. Future metadata stays outside application layouts.
use crate::{traces, Runtime, SyncGuard, ACTIVE};
use std::{
    collections::HashMap,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{atomic::Ordering, Arc},
};

#[derive(Clone, Copy, Hash, PartialEq, Eq)]
struct Key {
    address: usize,
    site: u128,
}
#[derive(Clone, Copy)]
struct Entry {
    token: u64,
    context: Option<traces::Context>,
}
#[derive(Default)]
pub(crate) struct Registry {
    entries: HashMap<Key, Entry>,
    exhausted: bool,
}
impl Registry {
    pub(crate) fn report(&self) -> serde_json::Value {
        serde_json::json!({"pending":self.entries.len(),"admission_exhausted":self.exhausted})
    }
    pub(crate) fn clear(&mut self) {
        self.entries.clear();
    }
}
enum Exit {
    Suspended,
    Completed,
    Cancelled,
}
/// A single synchronous poll/drop scope; it must never enter a future's fields.
#[must_use]
pub struct PollGuard {
    scope: Option<traces::Scope>,
    runtime: Option<Arc<Runtime>>,
    key: Key,
    exit: Exit,
}
impl PollGuard {
    fn inactive() -> Self {
        Self {
            scope: None,
            runtime: None,
            key: Key {
                address: 0,
                site: 0,
            },
            exit: Exit::Cancelled,
        }
    }
}
impl Drop for PollGuard {
    fn drop(&mut self) {
        let _sdk = crate::recording::Suppression::enter();
        self.scope.take();
        if matches!(self.exit, Exit::Suspended) {
            return;
        }
        let Some(runtime) = self.runtime.take() else {
            return;
        };
        // Release the registry before finishing a frame in the ordinary store.
        let entry = runtime
            .futures
            .lock()
            .ok()
            .and_then(|mut registry| registry.entries.remove(&self.key));
        if let Some(entry) = entry {
            if catch_unwind(AssertUnwindSafe(|| {
                runtime.finish(entry.token, matches!(self.exit, Exit::Cancelled));
            }))
            .is_err()
            {
                runtime.future_hook_failed();
            }
        }
    }
}
impl Runtime {
    fn future_hook_failed(&self) {
        if let Ok(mut data) = self.state.data.lock() {
            self.lose(&mut data, "invalid");
        }
    }
    fn rejected_future(&self, reason: &'static str) -> Option<traces::Context> {
        let mut data = self.state.data.lock().ok()?;
        self.lose(&mut data, reason);
        self.plan.traces.enabled.then(|| {
            data.traces
                .reject(self.owner, traces::current(self.owner), reason)
        })
    }
    fn future_scope(
        self: &Arc<Self>,
        key: Key,
        context: Option<traces::Context>,
        tracked: bool,
    ) -> PollGuard {
        PollGuard {
            scope: self
                .plan
                .traces
                .enabled
                .then(|| traces::Scope::attach(context)),
            runtime: tracked.then(|| self.clone()),
            key,
            exit: Exit::Cancelled,
        }
    }
    fn enter_future(self: &Arc<Self>, name: &str, key: Key) -> PollGuard {
        if crate::recording::Suppression::active() {
            return PollGuard::inactive();
        }
        let _sdk = crate::recording::Suppression::enter();
        if self.state.closed.load(Ordering::Acquire) {
            return PollGuard::inactive();
        }
        if key.address == 0 || key.site == 0 || name.is_empty() || name.len() > 1024 {
            return self.future_scope(key, self.rejected_future("invalid"), false);
        }
        let Ok(mut registry) = self.futures.lock() else {
            return self.future_scope(key, self.rejected_future("invalid"), false);
        };
        if let Some(entry) = registry.entries.get(&key) {
            return self.future_scope(key, entry.context, true);
        }
        if registry.exhausted || registry.entries.len() >= self.plan.runtime.max_active_calls {
            // A missing key could be a previously rejected suspended future.
            // Once identity storage is exhausted, do not later resample it as
            // a new root. Known entries may still complete and free storage.
            registry.exhausted = true;
            return self.future_scope(key, self.rejected_future("future_capacity"), false);
        }
        let mut guard = self.enter_inner(name);
        let entry = Entry {
            token: guard.token,
            context: guard.context,
        };
        // The registry owns metadata, not a Guard or an Arc back to Runtime.
        guard.runtime.take();
        registry.entries.insert(key, entry);
        self.future_scope(key, entry.context, true)
    }
    fn drop_future(self: &Arc<Self>, key: Key) -> PollGuard {
        if crate::recording::Suppression::active() {
            return PollGuard::inactive();
        }
        let _sdk = crate::recording::Suppression::enter();
        if self.state.closed.load(Ordering::Acquire) {
            return PollGuard::inactive();
        }
        let Ok(registry) = self.futures.lock() else {
            return self.future_scope(key, self.rejected_future("invalid"), false);
        };
        if let Some(entry) = registry.entries.get(&key) {
            self.future_scope(key, entry.context, true)
        } else if registry.exhausted {
            self.future_scope(key, self.rejected_future("future_capacity"), false)
        } else {
            // Never-polled and already-completed futures have no active body.
            PollGuard::inactive()
        }
    }
}

/// Enter a compiler-identified, already-pinned future body for one poll.
pub fn poll_enter(name: &str, site: u128, address: usize) -> PollGuard {
    catch_unwind(AssertUnwindSafe(|| {
        ACTIVE
            .get()
            .map(|runtime| runtime.enter_future(name, Key { address, site }))
            .unwrap_or_else(PollGuard::inactive)
    }))
    .unwrap_or_else(|_| {
        if let Some(runtime) = ACTIVE.get() {
            runtime.future_hook_failed();
        }
        PollGuard::inactive()
    })
}
/// Restore the caller's context; only Ready completes the whole async body.
pub fn poll_exit(mut guard: PollGuard, ready: bool) {
    guard.exit = if ready {
        Exit::Completed
    } else {
        Exit::Suspended
    };
}
/// Restore a suspended body's context while its existing drop glue executes.
pub fn drop_enter(site: u128, address: usize) -> PollGuard {
    catch_unwind(AssertUnwindSafe(|| {
        ACTIVE
            .get()
            .map(|runtime| runtime.drop_future(Key { address, site }))
            .unwrap_or_else(PollGuard::inactive)
    }))
    .unwrap_or_else(|_| {
        if let Some(runtime) = ACTIVE.get() {
            runtime.future_hook_failed();
        }
        PollGuard::inactive()
    })
}
/// Complete cancellation after the application's original drop glue finishes.
pub fn drop_exit(guard: PollGuard) {
    drop(guard);
}
/// Complete an escaping panic without altering its payload or caller context.
pub fn poll_unwind(guard: PollGuard) {
    drop(guard);
}

/// Enter a synchronous compiler body without allowing an SDK unwind to escape.
pub fn sync_enter(name: &str) -> SyncGuard {
    catch_unwind(AssertUnwindSafe(|| crate::enter(name))).unwrap_or_else(|_| {
        if let Some(runtime) = ACTIVE.get() {
            runtime.future_hook_failed();
        }
        SyncGuard {
            scope: None,
            guard: crate::Guard {
                runtime: None,
                token: 0,
                context: None,
            },
        }
    })
}
/// Restore caller context and record the synchronous return or original unwind.
pub fn sync_exit(guard: SyncGuard) {
    drop(guard);
}

/// Install the resolved runtime before the application's original main body.
pub fn start() {
    std::mem::forget(crate::launch());
}
/// Flush after the application's normal or unwinding main-body cleanup.
pub fn stop() {
    drop(crate::Shutdown);
}

#[cfg(test)]
#[path = "tests/compiler_mir.rs"]
mod tests;

/// Stack-only exclusion for global allocator implementations and their helpers.
/// Recording could allocate while an allocator already holds its own lock.
#[must_use]
pub struct AllocatorGuard {
    _suppression: crate::recording::Suppression,
}
pub fn allocation_enter() -> AllocatorGuard {
    AllocatorGuard {
        _suppression: crate::recording::Suppression::enter(),
    }
}
pub fn allocation_exit(guard: AllocatorGuard) {
    drop(guard);
}
