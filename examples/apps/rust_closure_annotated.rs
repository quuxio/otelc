fn configured() -> i32 {
    let callback = |value| value + 7;
    callback(3)
}
fn main() {
    // otelc.instrument
    let selected = |value| value * 3;
    // otelc.exclude
    let excluded = |value| value - 1;
    println!("{}", selected(4) + configured() + excluded(9));
}
