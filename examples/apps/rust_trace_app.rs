use std::{future::Future, pin::Pin, task::{Context, Poll, Waker}};
struct YieldOnce(bool);
impl Future for YieldOnce {
    type Output=();
    fn poll(mut self:Pin<&mut Self>,_:&mut Context<'_>)->Poll<()> {
        if self.0 {Poll::Ready(())} else {self.0=true;Poll::Pending}
    }
}
fn recursive(depth:u32)->u32 { if depth==0 {0} else {1+recursive(depth-1)} }
async fn child()->u32 {YieldOnce(false).await;42}
async fn parent()->u32 {child().await}
async fn cancelled() {std::future::pending::<()>().await;}
fn escaping() {std::panic::panic_any(String::from("original payload"));}
fn main() {
    assert_eq!(recursive(3),3);
    let mut task=Box::pin(parent()); let mut cx=Context::from_waker(Waker::noop());
    assert!(task.as_mut().poll(&mut cx).is_pending());
    let result=std::thread::spawn(move ||task.as_mut().poll(&mut Context::from_waker(Waker::noop()))).join().unwrap();
    assert_eq!(result,Poll::Ready(42));
    let mut task=Box::pin(cancelled()); assert!(task.as_mut().poll(&mut cx).is_pending()); drop(task);
    std::panic::set_hook(Box::new(|_|{}));
    let payload=std::panic::catch_unwind(escaping).unwrap_err();
    assert_eq!(*payload.downcast::<String>().unwrap(),"original payload");
    println!("trace results preserved");
}
