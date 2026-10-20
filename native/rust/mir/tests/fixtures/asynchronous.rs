use std::{future::Future, pin::Pin, sync::{Arc, atomic::{AtomicUsize, Ordering}}, task::{Context, Poll, Wake, Waker}};
static DROPS: AtomicUsize = AtomicUsize::new(0);
struct Noop;
impl Wake for Noop { fn wake(self: Arc<Self>) {} }
struct Pending;
impl Future for Pending {
    type Output = u32;
    fn poll(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<u32> { Poll::Pending }
}
impl Drop for Pending { fn drop(&mut self) { DROPS.fetch_add(1, Ordering::Relaxed); } }
async fn selected_ready() -> u32 { column!() }
async fn selected_pending() -> u32 { Pending.await }
async fn selected_parent() -> u32 { selected_pending().await }
fn require_send<T: Send>(_: &T) {}
fn layout<T>(_: &T) -> (usize, usize, bool) { (std::mem::size_of::<T>(), std::mem::align_of::<T>(), std::mem::needs_drop::<T>()) }
fn main() {
    let waker = Waker::from(Arc::new(Noop));
    let mut context = Context::from_waker(&waker);
    drop(selected_ready());
    let mut ready = Box::pin(selected_ready());
    println!("ready layout {:?}", layout(ready.as_ref().get_ref()));
    require_send(&ready);
    let result = ready.as_mut().poll(&mut context);
    let mut pending = Box::pin(selected_pending());
    println!("pending layout {:?}", layout(pending.as_ref().get_ref()));
    require_send(&pending);
    assert!(pending.as_mut().poll(&mut context).is_pending());
    assert!(pending.as_mut().poll(&mut context).is_pending());
    std::thread::spawn(move || drop(pending)).join().unwrap();
    let mut parent = Box::pin(selected_parent());
    println!("parent layout {:?}", layout(parent.as_ref().get_ref()));
    assert!(parent.as_mut().poll(&mut context).is_pending());
    assert!(parent.as_mut().poll(&mut context).is_pending());
    std::thread::spawn(move || drop(parent)).join().unwrap();
    println!("{result:?} drops {}", DROPS.load(Ordering::Relaxed));
}
