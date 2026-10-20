//! Exercise compiler-generated exits and ordinary unselected thread closures.
use super::*;

#[test]
#[ignore = "requires the qualified nightly and make rust-llvm-build"]
fn unselected_worker_closures_do_not_block_selected_functions_or_leak_thread_context() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy_matching(
        root,
        &receiver,
        vec!["app.work".into(), "app.leaf".into(), "app.fail".into()],
    );
    let source = r#"fn leaf(v:u64)->u64{v+1}
fn work(v:u64)->u64{leaf(v)}
fn fail(){std::panic::panic_any(String::from("original"));}
fn main(){std::panic::set_hook(Box::new(|_|{}));let error=std::panic::catch_unwind(fail).unwrap_err();let threads:Vec<_>=(0..4).map(|v|std::thread::spawn(move ||work(v))).collect();let sum:u64=threads.into_iter().map(|t|t.join().unwrap()).sum();println!("{}:{sum}",*error.downcast::<String>().unwrap());}
"#;
    fs::write(root.join("app.rs"), source).unwrap();
    let metadata = build(root, &plan);
    let output = success(compare(root, &plan, &metadata));
    assert_eq!(output.stdout, b"original:10\n");
    let report = health(root, 9);
    assert_eq!(report["traces"]["active_trees"], 0);
    let spans = receiver.spans();
    assert_eq!(spans.len(), 9);
    let roots: Vec<_> = spans
        .iter()
        .filter(|span| span.parent_span_id.is_empty())
        .collect();
    assert_eq!(roots.len(), 5);
    assert_eq!(
        roots
            .iter()
            .map(|span| &span.trace_id)
            .collect::<std::collections::BTreeSet<_>>()
            .len(),
        5
    );
    assert_eq!(
        spans
            .iter()
            .filter(|span| span.status.as_ref().is_some_and(|status| status.code == 2))
            .count(),
        1
    );
    assert_eq!(
        spans.iter().filter(|span| span.name == "app.work").count(),
        4
    );
    assert_eq!(
        spans.iter().filter(|span| span.name == "app.leaf").count(),
        4
    );
    assert_eq!(fs::read_to_string(root.join("app.rs")).unwrap(), source);
}

#[test]
#[ignore = "requires the qualified nightly and make rust-llvm-build"]
fn escaping_panic_closes_selected_main_and_child_without_changing_channels_or_status() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy_configured(
        root,
        &receiver,
        vec!["app.main".into(), "app.fail".into()],
        vec![],
        4096,
    );
    let source = r#"fn fail(){std::panic::panic_any(String::from("original"));}
fn main(){std::panic::set_hook(Box::new(|i|{let l=i.location().unwrap();eprintln!("panic@{}:{}:{}",l.file(),l.line(),l.column());}));println!("before");fail();}
"#;
    fs::write(root.join("app.rs"), source).unwrap();
    let metadata = build(root, &plan);
    let output = compare(root, &plan, &metadata);
    assert_eq!(output.status.code(), Some(101));
    assert_eq!(output.stdout, b"before\n");
    let report = health(root, 2);
    assert_eq!(report["functions"]["app.main"]["unwinds"], 1);
    assert_eq!(report["functions"]["app.fail"]["unwinds"], 1);
    let spans = receiver.spans();
    assert_eq!(spans.len(), 2);
    assert!(spans
        .iter()
        .all(|span| span.status.as_ref().unwrap().code == 2));
    let main = spans.iter().find(|span| span.name == "app.main").unwrap();
    let child = spans.iter().find(|span| span.name == "app.fail").unwrap();
    assert!(main.parent_span_id.is_empty());
    assert_eq!(child.parent_span_id, main.span_id);
    assert_eq!(fs::read_to_string(root.join("app.rs")).unwrap(), source);
}

#[test]
#[ignore = "requires the qualified nightly and make rust-llvm-build"]
fn rejected_nested_scope_discards_partial_tree_and_allows_a_later_healthy_root() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy_configured(
        root,
        &receiver,
        vec!["app.outer".into(), "app.child".into(), "app.healthy".into()],
        vec!["*.main".into()],
        1,
    );
    let source = "fn child()->u64{5}\nfn outer()->u64{child()}\nfn healthy()->u64{9}\nfn main(){println!(\"{}:{}\",outer(),healthy());}\n";
    fs::write(root.join("app.rs"), source).unwrap();
    let metadata = build(root, &plan);
    let output = success(compare(root, &plan, &metadata));
    assert_eq!(output.stdout, b"5:9\n");
    let report: Value =
        serde_json::from_slice(&fs::read(root.join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], 2);
    assert_eq!(report["export_finished"], true);
    assert_eq!(report["export_loss"], 0);
    assert_eq!(report["losses"]["active_call_capacity"], 1);
    assert_eq!(report["losses"]["invalid"], 0);
    assert_eq!(report["losses"]["incomplete"], 0);
    assert_eq!(report["traces"]["active_trees"], 0);
    assert_eq!(report["traces"]["queued_trees"], 0);
    let spans = receiver.spans();
    assert_eq!(spans.len(), 1);
    assert_eq!(spans[0].name, "app.healthy");
    assert!(spans[0].parent_span_id.is_empty());
    assert_eq!(fs::read_to_string(root.join("app.rs")).unwrap(), source);
}
