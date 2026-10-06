use super::*;
#[test]
fn publication_and_reuse() {
    loom::model(|| {
        let queue = loom::sync::Arc::new(Queue::new(2));
        let producer = queue.clone();
        let handle = loom::thread::spawn(move || {
            for i in 1..=3 {
                while !producer.push(Record {
                    invocation: i,
                    ..Default::default()
                }) {
                    loom::thread::yield_now();
                }
            }
        });
        for i in 1..=3 {
            let record = loop {
                if let Some(record) = queue.pop() {
                    break record;
                }
                loom::thread::yield_now();
            };
            assert_eq!(record.invocation, i);
        }
        handle.join().unwrap();
        assert!(queue.empty());
    });
}
