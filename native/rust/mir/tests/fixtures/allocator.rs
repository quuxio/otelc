use std::alloc::{GlobalAlloc, Layout, System};

use std::sync::atomic::{AtomicBool, Ordering};
static LOCK: AtomicBool = AtomicBool::new(false);
struct Allocator;
unsafe impl GlobalAlloc for Allocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        while LOCK.compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed).is_err() {
            std::hint::spin_loop();
        }
        let pointer = unsafe { selected_allocate(layout) };
        LOCK.store(false, Ordering::Release);
        pointer
    }
    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        unsafe { System.dealloc(pointer, layout) }
    }
}
#[global_allocator]
static ALLOCATOR: Allocator = Allocator;
#[inline(never)]
unsafe fn selected_allocate(layout: Layout) -> *mut u8 {
    unsafe { System.alloc(layout) }
}
#[inline(never)]
fn selected_work() -> usize {
    std::hint::black_box(vec![1_u8; 128]).len()
}
fn main() {
    println!("{}", selected_work());
}
