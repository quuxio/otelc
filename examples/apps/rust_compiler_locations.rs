#[track_caller]
fn selected() -> (u32, u32, &'static std::panic::Location<'static>) {
    (line!(), column!(), std::panic::Location::caller())
}

fn main() {
    println!("{:?}", selected());
}
