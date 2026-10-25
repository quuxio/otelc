use std::io::{self, BufRead, Write};
use std::process::ExitCode;
use std::time::Instant;

struct WorkItem {
    seed: i32,
}

impl Drop for WorkItem {
    fn drop(&mut self) {}
}

fn workload(seed: i32) -> u64 {
    assert!(seed >= 0, "negative seed");
    let item = WorkItem { seed };
    let mut value = item.seed as u64;
    let mut sum = 0;
    for _ in 0..128 {
        value = (value * 1664525 + 1013904223) & 65535;
        sum += value;
    }
    sum
}

fn excluded(seed: i32) -> u64 {
    seed as u64
}

fn main() -> ExitCode {
    for line in io::stdin().lock().lines() {
        let Ok(line) = line else {
            return ExitCode::from(2);
        };
        if line == "quit" {
            return ExitCode::SUCCESS;
        }
        let Ok(count) = line.parse::<i32>() else {
            return ExitCode::from(2);
        };
        if !(1..=1000000).contains(&count) {
            return ExitCode::from(2);
        }
        let start = Instant::now();
        let mut checksum = 0;
        for index in 0..count {
            checksum += workload(index) + excluded(0);
        }
        let elapsed = start.elapsed().as_nanos();
        println!("{{\"checksum\":{checksum},\"elapsed_ns\":{elapsed},\"calls\":{count}}}");
        if io::stdout().flush().is_err() {
            return ExitCode::from(2);
        }
    }
    ExitCode::SUCCESS
}
