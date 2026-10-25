use std::io::{self, BufRead};
use std::sync::atomic::{AtomicU32, Ordering};

static DROPS: AtomicU32 = AtomicU32::new(0);

struct Item {
    value: i32,
    fail: bool,
}

impl Drop for Item {
    fn drop(&mut self) {
        DROPS.fetch_add(1, Ordering::SeqCst);
        if self.fail {
            std::panic::panic_any(73_u32);
        }
    }
}

struct Zero;

impl Drop for Zero {
    fn drop(&mut self) {
        DROPS.fetch_add(1, Ordering::SeqCst);
    }
}

fn workload(command: i32) -> i32 {
    let item = Item {
        value: command * 7,
        fail: command == 2,
    };
    let first = Zero;
    let second = Zero;
    let moved = item;
    std::hint::black_box((&moved, &first, &second));
    if command == 3 {
        std::panic::panic_any(73_u32);
    }
    moved.value
}

fn main() {
    println!("ready");
    for line in io::stdin().lock().lines() {
        let value: i32 = line.unwrap().parse().unwrap();
        let result = match std::panic::catch_unwind(|| workload(value)) {
            Ok(value) => value,
            Err(payload) => {
                assert_eq!(payload.downcast_ref::<u32>(), Some(&73));
                -73
            }
        };
        println!("{} {}", result, DROPS.load(Ordering::SeqCst));
    }
}
