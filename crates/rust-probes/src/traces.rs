//! Rust synchronous and per-poll context scopes.
pub(crate) use quux_otelc_export::traces::{Context, Store};
use std::{cell::Cell, marker::PhantomData, rc::Rc};

thread_local! {
    static CURRENT: Cell<Option<Context>> = const { Cell::new(None) };
}
pub(crate) fn current(owner: u64) -> Option<Context> {
    CURRENT.with(|cell| cell.get().filter(|context| context.owner == owner))
}
/// Kept on the synchronous stack or inside a single poll, never across await.
pub(crate) struct Scope {
    previous: Option<Context>,
    _thread: PhantomData<Rc<()>>,
}
impl Scope {
    pub fn attach(context: Option<Context>) -> Self {
        Self {
            previous: CURRENT.with(|cell| cell.replace(context)),
            _thread: PhantomData,
        }
    }
}
impl Drop for Scope {
    fn drop(&mut self) {
        CURRENT.with(|cell| cell.set(self.previous));
    }
}
pin_project_lite::pin_project! {
    pub(crate) struct InContext<F> {
        #[pin]
        pub future: Option<F>,
        pub context: Option<Context>,
    }
    impl<F> PinnedDrop for InContext<F> {
        fn drop(this: Pin<&mut Self>) {
            let mut this = this.project();
            let _scope = Scope::attach(*this.context);
            this.future.set(None);
        }
    }
}
impl<F: std::future::Future> std::future::Future for InContext<F> {
    type Output = F::Output;
    fn poll(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Self::Output> {
        let this = self.project();
        let _scope = Scope::attach(*this.context);
        this.future
            .as_pin_mut()
            .expect("future present until drop")
            .poll(cx)
    }
}

#[cfg(test)]
#[path = "tests/trace_scope.rs"]
mod tests;
