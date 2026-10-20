//! Keep allocations made by the SDK out of application instrumentation.
use std::{cell::Cell, marker::PhantomData, rc::Rc};

thread_local! {
    static SUPPRESSED: Cell<bool> = const { Cell::new(false) };
}

// Thread-local state is restored on normal return and unwind. The guard cannot
// cross threads, and entering it neither allocates nor initialises heap TLS.
pub(crate) struct Suppression {
    previous: bool,
    _thread: PhantomData<Rc<()>>,
}
impl Suppression {
    pub(crate) fn enter() -> Self {
        Self {
            previous: SUPPRESSED.with(|state| state.replace(true)),
            _thread: PhantomData,
        }
    }
    pub(crate) fn active() -> bool {
        SUPPRESSED.try_with(Cell::get).unwrap_or(true)
    }
}
impl Drop for Suppression {
    fn drop(&mut self) {
        let _ = SUPPRESSED.try_with(|state| state.set(self.previous));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nested_operations_restore_state_across_threads_and_unwind() {
        assert!(!Suppression::active());
        let outer = Suppression::enter();
        assert!(Suppression::active());
        let original = std::panic::catch_unwind(|| {
            let _nested = Suppression::enter();
            assert!(Suppression::active());
            std::panic::panic_any(17_u8);
        })
        .unwrap_err();
        assert_eq!(original.downcast_ref::<u8>(), Some(&17));
        assert!(Suppression::active());
        std::thread::spawn(|| assert!(!Suppression::active()))
            .join()
            .unwrap();
        drop(outer);
        assert!(!Suppression::active());
    }
}
