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
