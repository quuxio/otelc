// otelc.instrument
fn annotated(value: i32) -> i32 { value * 3 }
fn configured(value: i32) -> i32 { value * 3 }
// otelc.exclude
fn excluded(value: i32) -> i32 { value * 3 }
fn main() { println!("{}", annotated(2) + configured(3) + excluded(5)); }
