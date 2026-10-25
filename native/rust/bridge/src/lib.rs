//! Private compiler hook bridge; developer applications do not import this crate.
//!
//! The tooling owns SDK initialisation, selection and linking. Hook calls use
//! the existing native pipeline and allocate no Rust-side registry or exporter.

use std::sync::atomic::{AtomicUsize, Ordering};

static LAST_KEY: AtomicUsize = AtomicUsize::new(0);

// Keys identify observations, not storage. Relaxed ordering supplies uniqueness;
// SDK admission and ownership remain synchronised at their existing boundaries.
fn next_key(counter: &AtomicUsize) -> Option<usize> {
    counter
        .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
            value.checked_add(1)
        })
        .ok()
        .map(|previous| previous + 1)
}

#[cfg(test)]
#[path = "keys_tests.rs"]
mod keys_tests;

mod native {
    unsafe extern "C" {
        pub fn otelc_function_named_enter_v1(name: *const u8, size: u64) -> u64;
        pub fn otelc_function_leave_v1(token: u64, unwinding: i32);
        pub fn otelc_object_created_v1(address: *const u8, type_usr: *const u8, name: *const u8);
        pub fn otelc_object_relocated_v1(
            source: *const u8,
            destination: *const u8,
            type_usr: *const u8,
        );
        pub fn otelc_object_destroying_v1(address: *const u8, type_usr: *const u8) -> u64;
        pub fn otelc_object_destroyed_v1(token: u64, unwinding: i32);
        pub fn otelc_object_metadata_error_v1();
    }
}

/// Non-generic typed entry points resolved by the pinned compiler driver.
pub mod compiler_mir {
    // Compiler-owned identities include their terminator. Validate borrowed
    // bytes without allocation, panics or reading application object storage.
    fn terminated(value: &str) -> bool {
        let bytes = value.as_bytes();
        (2..=4097).contains(&bytes.len())
            && bytes.last() == Some(&0)
            && !bytes[..bytes.len() - 1].contains(&0)
    }

    /// Allocate an identity independent of object size, address or ownership moves.
    #[inline(never)]
    pub fn drop_new(type_usr: &str, name: &str) -> usize {
        if terminated(type_usr)
            && terminated(name)
            && let Some(key) = super::next_key(&super::LAST_KEY)
        {
            // The SDK treats this integer as opaque and never reads its address.
            unsafe {
                super::native::otelc_object_created_v1(
                    key as *const u8,
                    type_usr.as_ptr(),
                    name.as_ptr(),
                )
            }
            key
        } else {
            unsafe { super::native::otelc_object_metadata_error_v1() }
            0
        }
    }

    /// Observe successful construction; storage is an opaque compiler identity.
    #[inline(never)]
    pub fn drop_created(storage: usize, type_usr: &str, name: &str) {
        if terminated(type_usr) && terminated(name) {
            // The native boundary copies metadata and never dereferences storage.
            unsafe {
                super::native::otelc_object_created_v1(
                    storage as *const u8,
                    type_usr.as_ptr(),
                    name.as_ptr(),
                )
            }
        } else {
            unsafe { super::native::otelc_object_metadata_error_v1() }
        }
    }

    /// Transfer the existing observation after a successful ownership move.
    #[inline(never)]
    pub fn drop_moved(source: usize, destination: usize, type_usr: &str) {
        if terminated(type_usr) {
            unsafe {
                super::native::otelc_object_relocated_v1(
                    source as *const u8,
                    destination as *const u8,
                    type_usr.as_ptr(),
                )
            }
        } else {
            unsafe { super::native::otelc_object_metadata_error_v1() }
        }
    }

    /// Claim the actual drop entry; zero means no observation was admitted.
    #[inline(never)]
    pub fn drop_enter(storage: usize, type_usr: &str) -> u64 {
        if terminated(type_usr) {
            unsafe {
                super::native::otelc_object_destroying_v1(storage as *const u8, type_usr.as_ptr())
            }
        } else {
            unsafe { super::native::otelc_object_metadata_error_v1() }
            0
        }
    }

    /// Complete the original normal drop without owning the application value.
    #[inline(never)]
    pub fn drop_exit(token: u64) {
        unsafe { super::native::otelc_object_destroyed_v1(token, 0) }
    }

    /// Complete an existing drop unwind without inspecting its panic payload.
    #[inline(never)]
    pub fn drop_unwind(token: u64) {
        unsafe { super::native::otelc_object_destroyed_v1(token, 1) }
    }

    /// Admit a compiler-selected function and copy its identity into SDK storage.
    #[inline(never)]
    pub fn sync_enter(name: &str) -> u64 {
        // The valid Rust string remains borrowed throughout this call. The
        // noexcept C++ boundary owns any retained copy and all admission state.
        unsafe { super::native::otelc_function_named_enter_v1(name.as_ptr(), name.len() as u64) }
    }

    /// Complete the original normal return without owning application values.
    #[inline(never)]
    pub fn sync_exit(token: u64) {
        // Tokens originate at the matching native entry; zero is a refused
        // admission and the native boundary treats its completion as a no-op.
        unsafe { super::native::otelc_function_leave_v1(token, 0) }
    }

    /// Observe an existing unwind; do not catch, replace or format its payload.
    #[inline(never)]
    pub fn sync_unwind(token: u64) {
        unsafe { super::native::otelc_function_leave_v1(token, 1) }
    }
}
