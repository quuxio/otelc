macro_rules! make_function {
    ($name:ident) => {
        fn $name(value: u32) -> u32 {
            value + 1
        }
    };
}

make_function!(generated);

fn generic<T: Copy>(value: T) -> T {
    value
}

fn main() {
    println!("{}:{}:{}", generated(4), generic(6_u32), generic(7_u64));
}
