use std::{
    future::Future,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    task::{Context, Poll, Waker},
};

struct YieldOnce(bool);
impl Future for YieldOnce {
    type Output = ();
    fn poll(mut self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<()> {
        if self.0 {
            Poll::Ready(())
        } else {
            self.0 = true;
            Poll::Pending
        }
    }
}
struct Cleanup(Arc<AtomicUsize>);
impl Drop for Cleanup {
    fn drop(&mut self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}

async fn ready(value: i32) -> i32 {
    value + 1
}
async fn borrowed(value: &mut String) -> &str {
    YieldOnce(false).await;
    value.push('!');
    value.as_str()
}
async fn waiting(cleanups: Arc<AtomicUsize>) {
    let _cleanup = Cleanup(cleanups);
    std::future::pending::<()>().await;
}
async fn migrating(cleanups: Arc<AtomicUsize>) -> i32 {
    let _cleanup = Cleanup(cleanups);
    YieldOnce(false).await;
    7
}
async fn escaping() {
    std::panic::panic_any(String::from("original payload"));
}
async fn recovered() -> i32 {
    let result = std::panic::catch_unwind(|| panic!("caught"));
    if result.is_err() {
        return 9;
    }
    0
}
async fn fallible() -> Result<i32, &'static str> {
    Err::<(), _>("original error")?;
    Ok(1)
}
struct Ordered(&'static str, Arc<Mutex<Vec<&'static str>>>);
impl Drop for Ordered {
    fn drop(&mut self) {
        self.1.lock().unwrap().push(self.0);
    }
}
async fn ordered(first: Ordered, second: Ordered) {
    let _local = Ordered("local", first.1.clone());
    assert_ne!(second.0, first.0);
    std::future::pending::<()>().await;
}
async fn callable(early: bool) -> fn() -> i32 {
    if early {
        return || 11;
    }
    || 12
}
async fn mutable(mut value: i32) -> i32 {
    value += 1;
    value
}
async fn generic<T>(value: T) -> T {
    value
}
async fn opaque() -> impl std::fmt::Display {
    "opaque"
}
struct AsyncOrder(i32);
impl AsyncOrder {
    async fn calculate(&mut self) -> i32 {
        YieldOnce(false).await;
        self.0 += 1;
        self.0
    }
}

fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let cleanups = Arc::new(AtomicUsize::new(0));
    let mut context = Context::from_waker(Waker::noop());
    assert_eq!(
        std::pin::pin!(ready(2)).as_mut().poll(&mut context),
        Poll::Ready(3)
    );
    let mut text = String::from("borrowed");
    let mut borrowed = std::pin::pin!(borrowed(&mut text));
    assert!(borrowed.as_mut().poll(&mut context).is_pending());
    assert_eq!(
        borrowed.as_mut().poll(&mut context),
        Poll::Ready("borrowed!")
    );
    // An unpolled future has no admitted observation and never constructs Cleanup.
    drop(waiting(cleanups.clone()));
    let mut cancelled = Box::pin(waiting(cleanups.clone()));
    assert!(cancelled.as_mut().poll(&mut context).is_pending());
    std::thread::spawn(move || drop(cancelled)).join().unwrap();
    let mut moved = Box::pin(migrating(cleanups.clone()));
    assert!(moved.as_mut().poll(&mut context).is_pending());
    std::thread::spawn(move || {
        assert_eq!(
            moved.as_mut().poll(&mut Context::from_waker(Waker::noop())),
            Poll::Ready(7)
        );
    })
    .join()
    .unwrap();
    let payload = std::panic::catch_unwind(|| {
        let _ = std::pin::pin!(escaping())
            .as_mut()
            .poll(&mut Context::from_waker(Waker::noop()));
    })
    .unwrap_err();
    assert_eq!(
        payload.downcast_ref::<String>().unwrap(),
        "original payload"
    );
    assert_eq!(
        std::pin::pin!(recovered()).as_mut().poll(&mut context),
        Poll::Ready(9)
    );
    assert_eq!(
        std::pin::pin!(fallible()).as_mut().poll(&mut context),
        Poll::Ready(Err("original error"))
    );
    let drops = Arc::new(Mutex::new(Vec::new()));
    let mut ordered = Box::pin(ordered(
        Ordered("first", drops.clone()),
        Ordered("second", drops.clone()),
    ));
    assert!(ordered.as_mut().poll(&mut context).is_pending());
    drop(ordered);
    for (early, expected) in [(true, 11), (false, 12)] {
        let Poll::Ready(function) = std::pin::pin!(callable(early)).as_mut().poll(&mut context)
        else {
            panic!("immediate result required");
        };
        assert_eq!(function(), expected);
    }
    println!("drop order={:?}", drops.lock().unwrap());
    assert_eq!(
        std::pin::pin!(mutable(4)).as_mut().poll(&mut context),
        Poll::Ready(5)
    );
    let value = std::rc::Rc::new(13);
    let Poll::Ready(result) = std::pin::pin!(generic(value.clone()))
        .as_mut()
        .poll(&mut context)
    else {
        panic!("immediate result required");
    };
    assert!(std::rc::Rc::ptr_eq(&value, &result));
    let Poll::Ready(result) = std::pin::pin!(opaque()).as_mut().poll(&mut context) else {
        panic!("immediate result required");
    };
    assert_eq!(result.to_string(), "opaque");
    let mut order = AsyncOrder(2);
    let mut future = std::pin::pin!(order.calculate());
    assert!(future.as_mut().poll(&mut context).is_pending());
    assert_eq!(future.as_mut().poll(&mut context), Poll::Ready(3));
    println!(
        "async results preserved; cleanups={}",
        cleanups.load(Ordering::SeqCst)
    );
}
