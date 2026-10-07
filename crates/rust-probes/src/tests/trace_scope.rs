use super::*;
#[test]
fn thread_scopes_restore_on_unwind_and_do_not_leak_to_other_threads() {
    let mut store = Store::default();
    let root = store.enter(
        1,
        None,
        "root".into(),
        std::time::Instant::now(),
        &quux_otelc_config::Traces {
            root_sample_ratio: 1.0,
            ..Default::default()
        },
    );
    assert!(current(1).is_none());
    let _outer = Scope::attach(Some(root));
    assert!(current(1).is_some());
    assert!(current(2).is_none());
    std::thread::spawn(|| assert!(current(1).is_none()))
        .join()
        .unwrap();
    let payload = std::panic::catch_unwind(|| {
        let _nested = Scope::attach(None);
        assert!(current(1).is_none());
        panic!("original");
    })
    .unwrap_err();
    assert_eq!(*payload.downcast::<&str>().unwrap(), "original");
    assert!(current(1).is_some());
}
