//! Observation identity tests do not create or retain application objects.
use super::next_key;
use std::{
    collections::BTreeSet,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    thread,
};

#[test]
fn starts_at_one_and_never_reuses_an_observation() {
    let counter = AtomicUsize::new(0);
    for expected in 1..=1024 {
        assert_eq!(next_key(&counter), Some(expected));
    }
    assert_eq!(counter.load(Ordering::Relaxed), 1024);
}

#[test]
fn exhaustion_stays_exhausted_without_wrapping_to_zero() {
    let counter = AtomicUsize::new(usize::MAX - 1);
    assert_eq!(next_key(&counter), Some(usize::MAX));
    for _ in 0..3 {
        assert_eq!(next_key(&counter), None);
        assert_eq!(counter.load(Ordering::Relaxed), usize::MAX);
    }
}

#[test]
fn concurrent_allocations_are_unique_and_contiguous() {
    let counter = Arc::new(AtomicUsize::new(0));
    let workers: Vec<_> = (0..8)
        .map(|_| {
            let counter = Arc::clone(&counter);
            thread::spawn(move || {
                (0..256)
                    .map(|_| next_key(&counter).expect("small counter cannot exhaust"))
                    .collect::<Vec<_>>()
            })
        })
        .collect();
    let values: Vec<_> = workers
        .into_iter()
        .flat_map(|worker| worker.join().expect("key worker must finish"))
        .collect();
    assert_eq!(values.len(), 2048);
    assert_eq!(
        values.into_iter().collect::<BTreeSet<_>>(),
        (1..=2048).collect()
    );
    assert_eq!(counter.load(Ordering::Relaxed), 2048);
}
