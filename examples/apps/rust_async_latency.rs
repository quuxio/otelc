use std::io::{self, BufRead, Write};
use std::time::Instant;
#[inline(never)]
async fn process_order(value: u64) -> u64 {
    (value * 17 + 3) % 97
}
fn main() {
    println!("ready");
    io::stdout().flush().expect("ready output");
    for line in io::stdin().lock().lines() {
        let line = line.expect("application input");
        if line == "quit" {
            return;
        }
        let count: u64 = line
            .strip_prefix("batch ")
            .expect("batch request")
            .parse()
            .expect("batch count");
        let started = Instant::now();
        let mut checksum = 0;
        let mut context = std::task::Context::from_waker(std::task::Waker::noop());
        for index in 0..count {
            let mut future = std::pin::pin!(process_order(std::hint::black_box(index)));
            let std::task::Poll::Ready(result) =
                std::future::Future::poll(future.as_mut(), &mut context)
            else {
                panic!("immediate result required");
            };
            checksum += std::hint::black_box(result);
        }
        println!(
            "elapsed_ns={} checksum={checksum} calls={count}",
            started.elapsed().as_nanos()
        );
        io::stdout().flush().expect("batch output");
    }
}
