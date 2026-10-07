use super::*;
#[test]
fn frame_layout() {
    assert_eq!(std::mem::size_of::<Frame>(), 64);
}
#[test]
fn clocks() {
    assert!(monotonic() > 0);
    assert!(realtime() > 0);
}
#[test]
fn saturating_loss() {
    let c = AtomicU64::new(u64::MAX);
    increment(&c);
    assert_eq!(c.load(Ordering::Relaxed), u64::MAX);
}
#[test]
fn admission_reserves_only_up_to_the_global_limit() {
    let count = AtomicUsize::new(0);
    assert!(reserve_call(&count, 1));
    assert!(!reserve_call(&count, 1));
    count.fetch_sub(1, Ordering::AcqRel);
    assert!(reserve_call(&count, 1));
}
