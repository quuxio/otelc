#[path = "mir-module.rs"]
mod external;
fn selected_root() -> u32 { column!() }
macro_rules! generate {
    () => { fn selected_macro<T: Into<u32>>(value: T) -> u32 { value.into() + column!() } };
}
generate!();
#[inline(never)]
fn selected_loop(mut value: u32) -> u32 {
    let mut result = 0;
    while value != 0 { result += value; value -= 1; }
    result
}
#[inline(never)]
fn selected_panics() { panic!("preserved panic"); }
#[track_caller]
#[inline(never)]
fn selected_caller() -> (u32, u32) {
    let caller = std::panic::Location::caller();
    (caller.line(), caller.column())
}
fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    println!("{} {} {} {} {} {:?}", selected_root(), external::selected_module(), selected_macro(3u8), selected_loop(std::hint::black_box(10)), std::panic::catch_unwind(selected_panics).is_err(), selected_caller());
}
