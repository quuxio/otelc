//! Private compiler ABI: the guard stays in the compiled function's own stack.
use crate::{Guard, Runtime, Shutdown, SyncGuard, ACTIVE};
use std::{
    mem::{align_of, size_of, MaybeUninit},
    panic::{catch_unwind, AssertUnwindSafe},
    sync::Arc,
};

/// These values are part of the v1 LLVM-hook ABI, not application type layout.
pub(crate) const STORAGE_BYTES: usize = 256;
pub(crate) const STORAGE_ALIGN: usize = 16;

#[repr(C, align(16))]
struct Slot {
    active: bool,
    guard: MaybeUninit<SyncGuard>,
}
const _: () = assert!(size_of::<Slot>() <= STORAGE_BYTES);
const _: () = assert!(align_of::<Slot>() <= STORAGE_ALIGN);

fn invalid(runtime: Option<&Arc<Runtime>>) {
    if let Some(runtime) = runtime {
        if let Ok(mut data) = runtime.state.data.lock() {
            runtime.lose(&mut data, "invalid");
        }
    }
}

fn aligned(storage: *mut u8) -> bool {
    !storage.is_null() && (storage as usize).is_multiple_of(STORAGE_ALIGN)
}

/// # Safety
/// `storage` must be fresh writable aligned storage of STORAGE_BYTES bytes.
/// A non-null name must address `length` readable bytes for this call only.
pub(crate) unsafe fn begin(
    runtime: Option<&Arc<Runtime>>,
    name: *const u8,
    length: usize,
    storage: *mut u8,
) {
    if !aligned(storage) {
        invalid(runtime);
        return;
    }
    let slot = storage.cast::<Slot>();
    // Initialise before any SDK operation, so failed entry still has a safe exit.
    unsafe {
        slot.write(Slot {
            active: false,
            guard: MaybeUninit::uninit(),
        })
    };
    if name.is_null() || length == 0 || length > 1024 {
        invalid(runtime);
        return;
    }
    let bytes = unsafe { std::slice::from_raw_parts(name, length) };
    let Ok(name) = std::str::from_utf8(bytes) else {
        invalid(runtime);
        return;
    };
    let result = catch_unwind(AssertUnwindSafe(|| {
        let guard = if let Some(runtime) = runtime {
            runtime.enter_scoped(name)
        } else {
            SyncGuard {
                scope: None,
                guard: Guard {
                    context: None,
                    runtime: None,
                    token: 0,
                },
            }
        };
        unsafe {
            (*slot).guard.write(guard);
            (*slot).active = true;
        }
    }));
    if result.is_err() {
        invalid(runtime);
    }
}

/// # Safety
/// The compiler supplies a name and fresh stack storage satisfying `begin`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn otelc_rust_enter_v1(name: *const u8, length: usize, storage: *mut u8) {
    unsafe { begin(ACTIVE.get(), name, length, storage) };
}

/// # Safety
/// `storage` must have been initialised by enter and remain on the same thread.
/// Repeated exit is harmless while that storage is still live.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn otelc_rust_leave_v1(storage: *mut u8) {
    if !aligned(storage) {
        invalid(ACTIVE.get());
        return;
    }
    let slot = unsafe { &mut *storage.cast::<Slot>() };
    if slot.active {
        slot.active = false;
        let runtime = unsafe { slot.guard.assume_init_ref() }
            .guard
            .runtime
            .clone();
        if catch_unwind(AssertUnwindSafe(|| unsafe {
            slot.guard.assume_init_drop()
        }))
        .is_err()
        {
            invalid(runtime.as_ref());
        }
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn otelc_rust_start_v1() {
    // The compiler emits the matching shutdown hook on ordinary/unwinding main
    // exits. No application object or task is kept alive by this owner guard.
    std::mem::forget(crate::launch());
}

#[unsafe(no_mangle)]
pub extern "C" fn otelc_rust_shutdown_v1() {
    drop(Shutdown);
}
