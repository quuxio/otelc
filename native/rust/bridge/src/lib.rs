//! Private compiler hook bridge; developer applications do not import this crate.
//!
//! The tooling owns SDK initialisation, selection and linking. Hook calls use
//! the existing native pipeline and allocate no Rust-side registry or exporter.

mod native {
    unsafe extern "C" {
        pub fn otelc_function_named_enter_v1(name: *const u8, size: u64) -> u64;
        pub fn otelc_function_leave_v1(token: u64, unwinding: i32);
    }
}

/// Non-generic typed entry points resolved by the pinned compiler driver.
pub mod compiler_mir {
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
