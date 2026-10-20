use std::{future::Future, pin::Pin, sync::{Arc, atomic::{AtomicUsize, Ordering}}, task::{Context, Poll, Wake, Waker}};
static DROPS: AtomicUsize = AtomicUsize::new(0);
struct Noop;
impl Wake for Noop { fn wake(self: Arc<Self>) {} }
struct Pending(bool);
impl Future for Pending {
    type Output = ();
    fn poll(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<()> { Poll::Pending }
}
impl Drop for Pending {
    fn drop(&mut self) {
        DROPS.fetch_add(1, Ordering::Relaxed);
        if self.0 { std::panic::panic_any(31_u8); }
    }
}
async fn selected_pending() { Pending(false).await }
async fn selected_drop_panic() { Pending(true).await }
async fn selected_async_panic() { std::panic::panic_any(29_u8); }
fn selected_sync_panic() { std::panic::panic_any(23_u8); }
fn selected_ok() -> u32 { 7 }
fn original_panic(action: impl FnOnce(), expected: u8) {
    let payload = std::panic::catch_unwind(std::panic::AssertUnwindSafe(action)).unwrap_err();
    assert_eq!(payload.downcast_ref::<u8>(), Some(&expected));
}
fn poll<F: Future>(future: Pin<&mut F>) -> Poll<F::Output> {
    future.poll(&mut Context::from_waker(&Waker::from(Arc::new(Noop))))
}
struct Cleanup(Option<Pin<Box<dyn Future<Output = ()> + Send>>>);
impl Drop for Cleanup {
    fn drop(&mut self) {
        drop(self.0.take());
        original_panic(selected_sync_panic, 23);
        let mut failed = Box::pin(selected_async_panic());
        original_panic(|| { let _ = poll(failed.as_mut()); }, 29);
        drop(failed);
        let mut failed_drop = Box::pin(selected_drop_panic());
        assert!(poll(failed_drop.as_mut()).is_pending());
        original_panic(|| drop(failed_drop), 31);
        assert_eq!(selected_ok(), 7);
    }
}
fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let mut pending = Box::pin(selected_pending());
    println!("layout {} {} {}", std::mem::size_of_val(pending.as_ref().get_ref()), std::mem::align_of_val(pending.as_ref().get_ref()), needs_drop(pending.as_ref().get_ref()));
    assert!(poll(pending.as_mut()).is_pending());
    original_panic(|| { let _cleanup = Cleanup(Some(pending)); std::panic::panic_any(17_u8); }, 17);
    assert_eq!(selected_ok(), 7);
    assert_eq!(DROPS.load(Ordering::Relaxed), 2);
    println!("original payloads 17 23 29 31; drops {}; healthy {}", DROPS.load(Ordering::Relaxed), healthy_value());
}
fn healthy_value() -> u32 { 7 }

fn needs_drop<T>(_: &T) -> bool { std::mem::needs_drop::<T>() }
