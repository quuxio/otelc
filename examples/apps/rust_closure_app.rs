use std::sync::{Arc, Mutex};

struct Cleanup(&'static str, Arc<Mutex<Vec<&'static str>>>);
impl Drop for Cleanup {
    fn drop(&mut self) { self.1.lock().unwrap().push(self.0); }
}
fn apply<F: Fn(i32) -> i32>(function: F, value: i32) -> i32 { function(value) }
const fn factory() -> fn() -> i32 { || 7 }
fn main() {
    let double = |value| value * 2;
    assert_eq!(apply(double, 3), 6); assert_eq!(apply(double, 4), 8);
    let mut total = 0;
    let mut add = |value| { total += value; total };
    assert_eq!(add(2), 2); assert_eq!(add(3), 5);
    assert_eq!(total, 5);
    let pointer: fn(i32) -> i32 = |value| value + 1;
    assert_eq!(pointer(3), 4);
    let borrowed: fn(&str) -> &str = |value| value;
    assert_eq!(borrowed("borrowed"), "borrowed");
    let text = String::from("capture");
    let borrow = || text.as_str();
    assert_eq!(borrow(), "capture");
    let destructured = |(first, second): (i32, i32)| first + second;
    assert_eq!(destructured((2, 3)), 5);
    let drops = Arc::new(Mutex::new(Vec::new()));
    let pair = (Cleanup("first", drops.clone()), Cleanup("second", drops.clone()));
    let once = move || drop(pair.0);
    drop(pair.1); once();
    let capture = Cleanup("capture", drops.clone());
    let local_drops = drops.clone();
    let cleanup = move |argument: Cleanup| {
        let local = Cleanup("local", local_drops.clone());
        drop(capture); drop(local); drop(argument);
    };
    cleanup(Cleanup("argument", drops.clone()));
    let nested = || || 11;
    assert_eq!(nested()(), 11);
    let thread = std::thread::spawn(move || 13);
    assert_eq!(thread.join().unwrap(), 13);
    std::panic::set_hook(Box::new(|_| {}));
    let payload = String::from("original payload");
    let escaping = move || std::panic::panic_any(payload);
    let panic = std::panic::catch_unwind(escaping).unwrap_err();
    assert_eq!(panic.downcast_ref::<String>().unwrap(), "original payload");
    assert_eq!(factory()(), 7);
    let ownership = String::from("owned result");
    let take = move || ownership;
    assert_eq!(take(), "owned result");
    let early = |value| { if value { return 17; } 19 };
    assert_eq!(early(true), 17); assert_eq!(early(false), 19);
    let make_future = || std::future::ready(23);
    drop(make_future());
    let uncalled = || 19;
    assert_eq!(std::mem::size_of_val(&uncalled), 0);
    println!("closure results preserved; drops={:?}", *drops.lock().unwrap());
}
