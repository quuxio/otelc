use std::io::{self, BufRead, Write};
use std::time::Instant;
#[inline(never)]
fn process_order(value: u64) -> u64 { (value * 17 + 3) % 97 }
fn main() {
    println!("ready");
    io::stdout().flush().expect("ready output");
    for line in io::stdin().lock().lines() {
        let line = line.expect("application input");
        if line == "quit" { return; }
        let count: u64 = line.strip_prefix("batch ").expect("batch request").parse().expect("batch count");
        let started = Instant::now();
        let mut checksum = 0;
        for index in 0..count { checksum += std::hint::black_box(process_order(std::hint::black_box(index))); }
        println!("elapsed_ns={} checksum={checksum} calls={count}", started.elapsed().as_nanos());
        io::stdout().flush().expect("batch output");
    }
}
