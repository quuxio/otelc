use super::*;
#[test]
fn layout() {
    assert_eq!(std::mem::size_of::<Record>(), 64);
}
#[test]
fn full_empty_wrap() {
    let q = Queue::new(64);
    assert!(q.pop().is_none());
    for round in 0..20 {
        for i in 0..64 {
            assert!(q.push(Record {
                invocation: round * 64 + i,
                ..Default::default()
            }));
        }
        assert!(!q.push(Record::default()));
        for i in 0..64 {
            assert_eq!(q.pop().unwrap().invocation, round * 64 + i);
        }
        assert!(q.empty());
    }
}
#[test]
fn concurrent_publication() {
    let q = std::sync::Arc::new(Queue::new(64));
    let producer = q.clone();
    let handle = std::thread::spawn(move || {
        for i in 0..100000 {
            while !producer.push(Record {
                invocation: i,
                ..Default::default()
            }) {
                std::thread::yield_now();
            }
        }
    });
    for i in 0..100000 {
        let record = loop {
            if let Some(r) = q.pop() {
                break r;
            }
            std::thread::yield_now();
        };
        assert_eq!(record.invocation, i);
    }
    handle.join().unwrap();
}
