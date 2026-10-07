//! Native entry suppresses callbacks before Rust thread startup can allocate.
use std::{
    ffi::{c_void, CStr},
    io,
    mem::MaybeUninit,
};

type Task = Box<dyn FnOnce() + Send>;
extern "C" {
    fn otelc_start_worker(
        thread: *mut libc::pthread_t,
        name: *const libc::c_char,
        run: extern "C" fn(*mut c_void),
        argument: *mut c_void,
    ) -> libc::c_int;
}

pub(super) struct Handle(Option<libc::pthread_t>);
// The handle owns a joinable thread; its task is Send and owns its captures.
unsafe impl Send for Handle {}

impl Handle {
    pub fn join(mut self) -> io::Result<()> {
        let thread = self.0.take().expect("owned worker thread");
        let result = unsafe { libc::pthread_join(thread, std::ptr::null_mut()) };
        if result == 0 {
            Ok(())
        } else {
            Err(io::Error::from_raw_os_error(result))
        }
    }
}
impl Drop for Handle {
    fn drop(&mut self) {
        if let Some(thread) = self.0.take() {
            unsafe { libc::pthread_detach(thread) };
        }
    }
}

extern "C" fn run(argument: *mut c_void) {
    // C calls this once after installing thread-local suppression.
    let task = unsafe { Box::from_raw(argument.cast::<Task>()) };
    // Panics cannot unwind through the C entry. Normal completion signals remain
    // owned by each task, so a panic does not falsely report successful export.
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(task));
}

pub(super) fn spawn(
    name: &'static CStr,
    task: impl FnOnce() + Send + 'static,
) -> io::Result<Handle> {
    let argument = Box::into_raw(Box::new(Box::new(task) as Task)).cast();
    let mut thread = MaybeUninit::uninit();
    let result = unsafe { otelc_start_worker(thread.as_mut_ptr(), name.as_ptr(), run, argument) };
    if result != 0 {
        // Creation failed, so the C entry never took ownership of the task.
        drop(unsafe { Box::from_raw(argument.cast::<Task>()) });
        return Err(io::Error::from_raw_os_error(result));
    }
    Ok(Handle(Some(unsafe { thread.assume_init() })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{sync::mpsc, time::Duration};

    #[test]
    fn join_waits_for_completion_and_releases_task_captures() {
        let (tx, rx) = mpsc::channel();
        let handle = spawn(c"otelc-test", move || tx.send(42).unwrap()).unwrap();
        handle.join().unwrap();
        assert_eq!(rx.recv().unwrap(), 42);
        assert!(rx.recv().is_err());
    }

    #[test]
    fn dropping_handle_detaches_without_cancelling_task() {
        let (release, barrier) = mpsc::channel();
        let (tx, rx) = mpsc::channel();
        let handle = spawn(c"otelc-test", move || {
            barrier.recv().unwrap();
            tx.send(42).unwrap();
        })
        .unwrap();
        drop(handle);
        release.send(()).unwrap();
        assert_eq!(rx.recv_timeout(Duration::from_secs(2)).unwrap(), 42);
    }

    #[test]
    fn task_panic_does_not_unwind_across_native_entry_or_signal_completion() {
        let (tx, rx) = mpsc::channel::<()>();
        let handle = spawn(c"otelc-test", move || {
            let _held_until_unwind = tx;
            panic!("worker failure");
        })
        .unwrap();
        handle.join().unwrap();
        assert!(rx.recv().is_err());
    }
}
