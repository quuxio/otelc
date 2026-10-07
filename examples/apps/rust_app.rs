fn process_order(value: i32) -> i32 { value * 3 }
fn recursive(depth: i32) -> i32 {
    if depth == 0 { 0 } else { 1 + recursive(depth - 1) }
}
fn escaping() { std::panic::panic_any(String::from("escaping")); }
fn caught() -> i32 {
    std::panic::catch_unwind(|| std::panic::panic_any("caught")).map_or(7, |_| 0)
}
struct Order { value: i32 }
impl Order {
    fn calculate(&self, other: i32) -> i32 { self.value + other }
}
fn main() {
    let mut result = process_order(4) + recursive(3) + caught() + Order { value: 4 }.calculate(5);
    let panic = std::panic::catch_unwind(escaping).expect_err("escaping panic");
    result += panic.downcast_ref::<String>().expect("same payload").len() as i32;
    let workers: Vec<_> = [5, 6].into_iter().map(|value| std::thread::spawn(move || process_order(value))).collect();
    for worker in workers { result += worker.join().expect("worker result"); }
    println!("{result}");
}
