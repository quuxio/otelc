use super::*;
fn registry(capacity: usize) -> Registry {
    Registry::new(&quux_otelc_config::Objects {
        classes: vec!["OrderBook".into()],
        max_live: capacity,
    })
}
#[test]
fn capacity_reuse_and_invalid_tokens() {
    let objects = registry(1);
    assert!(objects.begin(b"unselected", 1).is_none());
    let first = objects.begin(b"OrderBook", 10).unwrap();
    assert_eq!(objects.active(), 1);
    assert!(objects.begin(b"OrderBook", 11).is_none());
    assert_eq!(objects.end(first, 20).unwrap().duration_tick, 10);
    assert!(objects.end(first, 21).is_none());
    let second = objects.begin(b"OrderBook", 30).unwrap();
    assert_ne!(first, second);
    assert!(objects.end(first, 31).is_none());
    assert_eq!(objects.end(second, 40).unwrap().duration_tick, 10);
    assert!(objects.end(0, 40).is_none());
    assert_eq!(objects.active(), 0);
}
#[test]
fn cross_thread_lifetime_and_duplicate_completion() {
    let objects = std::sync::Arc::new(registry(1));
    let token = objects.begin(b"OrderBook", 1).unwrap();
    let mut threads = Vec::new();
    for _ in 0..4 {
        let objects = objects.clone();
        threads.push(std::thread::spawn(move || objects.end(token, 5)));
    }
    assert_eq!(
        threads
            .into_iter()
            .filter_map(|t| t.join().unwrap())
            .count(),
        1
    );
    assert_eq!(objects.active(), 0);
}
#[test]
fn token_exhaustion_disables_admission() {
    let objects = registry(1);
    objects.next.store(u64::MAX, Ordering::Relaxed);
    assert!(objects.begin(b"OrderBook", 1).is_none());
}
