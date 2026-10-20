use super::*;
use crate::compiler_abi::{begin, otelc_rust_leave_v1, STORAGE_BYTES};
use crate::traces::current as context;

#[repr(align(16))]
struct Storage([u8; STORAGE_BYTES]);
impl Storage {
    fn new() -> Self {
        Self([0; STORAGE_BYTES])
    }
    fn pointer(&mut self) -> *mut u8 {
        self.0.as_mut_ptr()
    }
}

#[test]
fn compiler_slots_preserve_nested_context_and_complete_once() {
    let receiver = Receiver::good();
    let mut config = plan(&receiver);
    config.traces.enabled = true;
    config.traces.root_sample_ratio = 1.0;
    config.trace_export = Some(quux_otelc_config::common::TraceExport {
        endpoint: receiver.endpoint.replace("/v1/metrics", "/v1/traces"),
        protocol: "http/protobuf".into(),
        timeout_ms: 300,
    });
    let runtime = Runtime::new(config).unwrap();
    let mut root = Storage::new();
    let mut child = Storage::new();
    let mut sibling = Storage::new();
    unsafe { begin(Some(&runtime), b"root".as_ptr(), 4, root.pointer()) };
    assert!(context(runtime.owner).unwrap().sampled);
    unsafe { begin(Some(&runtime), b"child".as_ptr(), 5, child.pointer()) };
    assert!(context(runtime.owner).unwrap().sampled);
    unsafe {
        otelc_rust_leave_v1(child.pointer());
        otelc_rust_leave_v1(child.pointer());
    }
    unsafe {
        begin(Some(&runtime), b"sibling".as_ptr(), 7, sibling.pointer());
        otelc_rust_leave_v1(sibling.pointer());
    }
    unsafe { otelc_rust_leave_v1(root.pointer()) };
    assert!(context(runtime.owner).is_none());
    runtime.close();
    assert_eq!(runtime.report()["function_calls"], 3);
    assert_eq!(runtime.report()["losses"]["invalid"], 0);
    let received = receiver.traces.lock().unwrap();
    let spans: Vec<_> = received
        .iter()
        .flat_map(|request| &request.resource_spans)
        .flat_map(|resource| &resource.scope_spans)
        .flat_map(|scope| &scope.spans)
        .collect();
    assert_eq!(spans.len(), 3);
    let root = spans.iter().find(|span| span.name == "root").unwrap();
    let child = spans.iter().find(|span| span.name == "child").unwrap();
    assert_eq!(child.parent_span_id, root.span_id);
    assert_eq!(child.trace_id, root.trace_id);
    let sibling = spans.iter().find(|span| span.name == "sibling").unwrap();
    assert_eq!(sibling.parent_span_id, root.span_id);
    assert_eq!(sibling.trace_id, root.trace_id);
}

#[test]
fn compiler_slots_support_disabled_and_rejected_entries_without_leaking_context() {
    let receiver = Receiver::good();
    let mut config = plan(&receiver);
    config.runtime.max_active_calls = 1;
    let runtime = Runtime::new(config).unwrap();
    let mut first = Storage::new();
    let mut rejected = Storage::new();
    unsafe {
        begin(Some(&runtime), b"first".as_ptr(), 5, first.pointer());
        begin(Some(&runtime), b"rejected".as_ptr(), 8, rejected.pointer());
        otelc_rust_leave_v1(rejected.pointer());
        otelc_rust_leave_v1(first.pointer());
    }
    runtime.state.enabled.store(false, Ordering::Release);
    unsafe {
        begin(Some(&runtime), b"disabled".as_ptr(), 8, first.pointer());
        otelc_rust_leave_v1(first.pointer());
    }
    unsafe {
        begin(None, b"no_owner".as_ptr(), 8, first.pointer());
        otelc_rust_leave_v1(first.pointer());
    }
    runtime.close();
    assert_eq!(runtime.report()["function_calls"], 1);
    assert_eq!(runtime.report()["losses"]["active_call_capacity"], 1);
    assert!(context(runtime.owner).is_none());
}

#[test]
fn malformed_compiler_labels_leave_safe_slots_and_visible_diagnostics() {
    let receiver = Receiver::good();
    let runtime = Runtime::new(plan(&receiver)).unwrap();
    let mut storage = Storage::new();
    unsafe {
        begin(Some(&runtime), std::ptr::null(), 1, storage.pointer());
        otelc_rust_leave_v1(storage.pointer());
        begin(Some(&runtime), b"".as_ptr(), 0, storage.pointer());
        begin(Some(&runtime), b"x".as_ptr(), 1025, storage.pointer());
        begin(Some(&runtime), [0xff].as_ptr(), 1, storage.pointer());
        begin(Some(&runtime), b"x".as_ptr(), 1, std::ptr::null_mut());
        begin(Some(&runtime), b"x".as_ptr(), 1, storage.pointer().add(1));
    }
    assert_eq!(runtime.report()["losses"]["invalid"], 6);
    runtime.close();
}

#[test]
fn compiler_slots_restore_context_after_unwind_and_parallel_completion() {
    struct Exit<'a>(&'a mut Storage);
    impl Drop for Exit<'_> {
        fn drop(&mut self) {
            unsafe { otelc_rust_leave_v1(self.0.pointer()) };
        }
    }
    let receiver = Receiver::good();
    let runtime = Runtime::new(plan(&receiver)).unwrap();
    let payload = std::panic::catch_unwind(AssertUnwindSafe(|| {
        let mut storage = Storage::new();
        unsafe { begin(Some(&runtime), b"panic".as_ptr(), 5, storage.pointer()) };
        let _exit = Exit(&mut storage);
        std::panic::panic_any(String::from("original payload"));
    }))
    .unwrap_err();
    assert_eq!(*payload.downcast::<String>().unwrap(), "original payload");
    assert!(context(runtime.owner).is_none());
    let threads: Vec<_> = (0..4)
        .map(|_| {
            let runtime = Arc::clone(&runtime);
            std::thread::spawn(move || {
                let mut storage = Storage::new();
                unsafe {
                    begin(Some(&runtime), b"parallel".as_ptr(), 8, storage.pointer());
                    otelc_rust_leave_v1(storage.pointer());
                }
                assert!(context(runtime.owner).is_none());
            })
        })
        .collect();
    for thread in threads {
        thread.join().unwrap();
    }
    runtime.close();
    assert_eq!(runtime.report()["functions"]["panic"]["unwinds"], 1);
    assert_eq!(runtime.report()["functions"]["parallel"]["count"], 4);
    assert_eq!(runtime.report()["losses"]["incomplete"], 0);
}
