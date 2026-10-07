use quux_otelc_config::common::{CommonConfig, Language};
use quux_otelc_rust::policy::Plan;
use quux_otelc_rust_adapter::transform::transform;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};
fn policy() -> Plan {
    serde_json::from_value(
        serde_json::to_value(
            CommonConfig::load(Path::new("../../examples/rust.toml"), true)
                .unwrap()
                .resolve(Language::Rust)
                .unwrap(),
        )
        .unwrap(),
    )
    .unwrap()
}
fn success(output: Output) -> Output {
    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    output
}
fn adapter() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_otelc-rust-adapter"))
}
fn run(root: &Path, plan: &Path, args: &[&str]) -> Output {
    Command::new(adapter())
        .arg(plan)
        .args(args)
        .current_dir(root)
        .env_remove("RUSTC_WRAPPER")
        .env_remove("RUSTC_WORKSPACE_WRAPPER")
        .env_remove("OTELC_RUST_GENERATED")
        .env("OTELC_REPORT_PATH", root.join("report.json"))
        .output()
        .unwrap()
}
fn write_plan(root: &Path) -> PathBuf {
    let mut value = serde_json::to_value(
        CommonConfig::load(Path::new("../../examples/rust.toml"), true)
            .unwrap()
            .resolve(Language::Rust)
            .unwrap(),
    )
    .unwrap();
    value["sources"]["include"] = serde_json::json!(["*.rs", "src/**"]);
    value["functions"]["include"] = serde_json::json!(["*"]);
    value["functions"]["exclude"] = serde_json::json!(["*.main", "*.excluded"]);
    value["export"]["interval_ms"] = serde_json::json!(60000);
    value["export"]["timeout_ms"] = serde_json::json!(40);
    value["runtime"]["shutdown_timeout_ms"] = serde_json::json!(100);
    value["metrics_endpoint"] = serde_json::json!("http://127.0.0.1:9/v1/metrics");
    let path = root.join("plan.json");
    fs::write(&path, value.to_string()).unwrap();
    path
}
#[test]
fn parser_respects_original_lines_byte_filters_annotations_and_hygiene() {
    let mut plan = policy();
    plan.functions.include = vec!["file.configured".into()];
    plan.functions.exclude = vec!["file.excluded".into()];
    let source = "// Unicode: λ and CRLF\r\n// otelc.instrument\r\nfn tagged(__quux_otelc_guard: i32) -> i32 { __quux_otelc_guard }\r\nfn configured() {}\r\n// otelc.instrument\r\nfn excluded() {}\r\n// otelc.instrument\r\n\r\nfn separate() {}\r\nfn main() {}\r\n";
    let (generated, functions) = transform(source, "file", &plan, true, true, false).unwrap();
    assert_eq!(generated.lines().count(), source.lines().count());
    assert!(generated.contains("let __quux_otelc_guard_="));
    assert!(generated.contains("/*otelc.instrument*/"));
    assert_eq!(
        functions
            .iter()
            .map(|function| function.selected)
            .collect::<Vec<_>>(),
        [true, true, false, false, false]
    );
    assert_eq!(functions[0].line, 3);
    assert!(generated.contains("::quux_otelc_rust::launch()"));
    assert!(transform(&generated, "file", &plan, true, true, false).is_err());
    let (excluded, _) = transform(source, "file", &plan, false, false, false).unwrap();
    assert_eq!(excluded, source);
    plan.annotations.read_existing = false;
    plan.annotations.inject_generated = false;
    let (generated, functions) = transform(source, "file", &plan, true, false, false).unwrap();
    assert!(!functions[0].selected);
    assert!(!generated.contains("/*otelc.instrument*/"));
    let (inspect, _) = transform(source, "file", &plan, true, false, true).unwrap();
    assert_eq!(inspect, source);
}
#[test]
fn named_scopes_generics_default_methods_nested_functions_and_raw_identifiers() {
    let mut plan = policy();
    plan.functions.include = vec!["*".into()];
    plan.functions.exclude.clear();
    let source = "mod book { pub fn price<T>(v:T)->T { fn nested() {} nested(); v } } struct Order; impl Order { fn r#type(&self) {} } trait Calculate { fn default_value(&self) -> i32 { 3 } fn abstract_value(&self); } impl Calculate for Order { fn abstract_value(&self) {} } impl<T> Calculate for [T; 3] { fn abstract_value(&self) {} }";
    let (generated, functions) = transform(source, "file", &plan, true, false, false).unwrap();
    syn::parse_file(&generated).unwrap();
    let names: Vec<_> = functions
        .iter()
        .map(|function| function.name.as_str())
        .collect();
    assert_eq!(
        names,
        [
            "file.book.price",
            "file.book.price.nested",
            "file.Order.r#type",
            "file.Calculate.default_value",
            "file.<Order as Calculate>.abstract_value",
            "file.<[T ; 3] as Calculate>.abstract_value"
        ]
    );
    plan.functions.include = vec!["file.λ?".into()];
    let (generated, functions) =
        transform("fn λx() {}", "file", &plan, true, false, false).unwrap();
    assert!(functions[0].selected);
    syn::parse_file(&generated).unwrap();
}
#[test]
fn unsupported_functions_are_inspectable_and_rejected_when_selected() {
    let mut plan = policy();
    plan.functions.include = vec!["*".into()];
    plan.functions.exclude.clear();
    for source in [
        "const fn fixed() -> i32 { 2 }",
        "async fn patterned((x, y): (i32, i32)) -> i32 { x + y }",
        "async fn referenced(ref value: String) -> usize { value.len() }",
        "#[unsafe(naked)] unsafe extern \"C\" fn bare() {}",
    ] {
        assert!(transform(source, "file", &plan, true, false, false).is_err());
        let (inspected, functions) = transform(source, "file", &plan, true, false, true).unwrap();
        assert_eq!(inspected, source);
        assert!(functions[0].unsupported.is_some());
        assert!(transform(source, "file", &plan, false, false, false).is_ok());
    }
    assert!(transform("async fn main() {}", "file", &plan, false, true, false).is_err());
    for source in [
        "include!(\"part.rs\");",
        "// otelc.unknown\nfn wrong() {}",
        "fn broken(",
    ] {
        assert!(transform(source, "file", &plan, true, false, false).is_err());
    }
    assert!(transform(
        "// otelc.unknown\nfn wrong() {}",
        "file",
        &plan,
        true,
        false,
        true
    )
    .is_err());
    let (_, functions) = transform(
        "/// otelc.instrument\nfn doc() {}\n// otelc.exclude\nfn excluded() {}",
        "file",
        &plan,
        true,
        false,
        false,
    )
    .unwrap();
    assert!(functions[0].selected);
    assert!(!functions[1].selected);
}

#[test]
fn async_futures_preserve_results_borrows_panics_cancellation_and_thread_movement() {
    let root = tempfile::tempdir().unwrap();
    let source = include_str!("../../../examples/apps/rust_async_app.rs");
    let input = root.path().join("main.rs");
    fs::write(&input, source).unwrap();
    let binary = root.path().join("plain");
    success(
        Command::new("rustc")
            .args(["--edition=2024", "-O"])
            .arg(&input)
            .arg("-o")
            .arg(&binary)
            .output()
            .unwrap(),
    );
    let baseline = success(Command::new(&binary).output().unwrap());
    let path = write_plan(root.path());
    let mut plan: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    plan["functions"]["include"] = serde_json::json!([
        "main.ready",
        "main.borrowed",
        "main.waiting",
        "main.migrating",
        "main.escaping",
        "main.recovered",
        "main.fallible",
        "main.ordered",
        "main.callable",
        "main.mutable",
        "main.generic",
        "main.opaque",
        "main.AsyncOrder.calculate"
    ]);
    fs::write(&path, plan.to_string()).unwrap();
    let instrumented = success(run(root.path(), &path, &["main.rs"]));
    assert_eq!(baseline.stdout, instrumented.stdout);
    assert_eq!(baseline.stderr, instrumented.stderr);
    assert_eq!(fs::read_to_string(input).unwrap(), source);
    let report: serde_json::Value =
        serde_json::from_slice(&fs::read(root.path().join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], 14);
    assert_eq!(report["functions"]["main.waiting"]["cancellations"], 1);
    assert_eq!(report["functions"]["main.escaping"]["unwinds"], 1);
    assert_eq!(report["functions"]["main.recovered"]["unwinds"], 0);
    assert!(report["losses"]
        .as_object()
        .unwrap()
        .values()
        .all(|count| count == 0));
}
#[test]
fn specialised_receivers_and_nested_trait_methods_have_distinct_identities() {
    let mut plan = policy();
    plan.functions.include = vec!["*".into()];
    plan.functions.exclude.clear();
    let source = "struct Slot<T>(T); impl Slot<u32> { fn value(&self) {} } impl Slot<u64> { fn value(&self) {} } impl first::Order { fn value(&self) {} } impl second::Order { fn value(&self) {} } trait Defaulted { fn first(&self) { fn helper() {} helper(); } fn second(&self) { fn helper() {} helper(); } } fn marker_text()->&'static str { \"quux.otelc.generated\" }";
    let (_, functions) = transform(source, "file", &plan, true, false, false).unwrap();
    let names: std::collections::HashSet<_> = functions
        .iter()
        .map(|function| function.name.as_str())
        .collect();
    assert_eq!(names.len(), functions.len());
    assert!(names.contains("file.Defaulted.first.helper"));
    assert!(names.contains("file.Defaulted.second.helper"));
    assert!(names.contains("file.Slot < u32 >.value"));
    assert!(names.contains("file.Slot < u64 >.value"));
}
#[test]
fn standalone_preserves_payload_drop_modules_assets_arguments_and_source() {
    let root = tempfile::tempdir().unwrap();
    let plan = write_plan(root.path());
    let source = "mod part; struct Cleanup; impl Drop for Cleanup { fn drop(&mut self) { assert!(std::thread::panicking()); } } fn escaping() { let _cleanup=Cleanup; std::panic::panic_any(String::from(\"original payload\")); } fn main() { let payload=std::panic::catch_unwind(escaping).unwrap_err(); assert_eq!(payload.downcast_ref::<String>().unwrap(), \"original payload\"); println!(\"{} {} {}:{}\", part::λx(), include_str!(\"asset.txt\"), file!(), line!()); assert_eq!(std::env::args().nth(1).unwrap(), \"argument\"); }";
    fs::write(root.path().join("main.rs"), source).unwrap();
    fs::write(root.path().join("part.rs"), "pub fn λx()->i32 { 42 }").unwrap();
    fs::write(root.path().join("asset.txt"), "asset").unwrap();
    success(
        Command::new("rustc")
            .args(["--edition=2024", "-O", "-g", "main.rs", "-o", "plain"])
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    let baseline = success(
        Command::new(root.path().join("plain"))
            .arg("argument")
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    let output = success(run(root.path(), &plan, &["main.rs", "argument"]));
    assert_eq!(output.stdout, baseline.stdout);
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("42 asset"));
    assert!(stdout.contains("main.rs:1"));
    assert!(!stdout.contains("/source/"));
    assert_eq!(
        fs::read_to_string(root.path().join("main.rs")).unwrap(),
        source
    );
    let report: serde_json::Value =
        serde_json::from_slice(&fs::read(root.path().join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], 3);
    assert_eq!(report["functions"]["main.escaping"]["unwinds"], 1);
    assert_eq!(
        report["functions"]["main.<Cleanup as Drop>.drop"]["unwinds"],
        0
    );
    assert_eq!(report["export_finished"], true);
    let inspected = success(run(root.path(), &plan, &["--inspect", "main.rs", "--json"]));
    let value: serde_json::Value = serde_json::from_slice(&inspected.stdout).unwrap();
    assert_eq!(value["language"], "rust");
    success(run(root.path(), &plan, &["--doctor"]));
    for args in [
        &[][..],
        &["--inspect"][..],
        &["--inspect", "main.rs", "wrong"][..],
        &["unknown"][..],
    ] {
        assert!(!run(root.path(), &plan, args).status.success());
    }
    fs::write(
        root.path().join("main.rs"),
        "fn main() { std::process::exit(7); }",
    )
    .unwrap();
    assert_eq!(run(root.path(), &plan, &["main.rs"]).status.code(), Some(7));
    fs::write(root.path().join("main.rs"), "fn main() { not_valid(); }").unwrap();
    assert!(!run(root.path(), &plan, &["main.rs"]).status.success());
}
#[test]
fn cargo_projects_keep_sources_manifests_lockfiles_and_build_scripts_unchanged() {
    let root = tempfile::tempdir().unwrap();
    let plan = write_plan(root.path());
    fs::create_dir(root.path().join("src")).unwrap();
    let manifest =
        "[package]\nname='otelc-fixture'\nversion='0.1.0'\nedition='2024'\n[workspace]\n";
    let main =
        "mod part; fn main() { println!(\"{} {}\", part::value(), env!(\"UNCHANGED_BUILD\")); }";
    let build = "fn main() { println!(\"cargo:rustc-env=UNCHANGED_BUILD=original\"); }";
    fs::write(root.path().join("Cargo.toml"), manifest).unwrap();
    fs::write(root.path().join("src/main.rs"), main).unwrap();
    fs::write(
        root.path().join("src/part.rs"),
        "pub fn value()->i32 { 42 }",
    )
    .unwrap();
    fs::write(root.path().join("build.rs"), build).unwrap();
    success(
        Command::new("cargo")
            .args(["generate-lockfile", "--offline"])
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    let lock = fs::read(root.path().join("Cargo.lock")).unwrap();
    let output = success(run(
        root.path(),
        &plan,
        &["Cargo.toml", "--bin", "otelc-fixture", "--", "argument"],
    ));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout).trim(),
        "42 original"
    );
    assert_eq!(
        fs::read_to_string(root.path().join("Cargo.toml")).unwrap(),
        manifest
    );
    assert_eq!(
        fs::read_to_string(root.path().join("src/main.rs")).unwrap(),
        main
    );
    assert_eq!(
        fs::read_to_string(root.path().join("build.rs")).unwrap(),
        build
    );
    assert_eq!(fs::read(root.path().join("Cargo.lock")).unwrap(), lock);
    assert!(!root.path().join("target").exists());
    let report: serde_json::Value =
        serde_json::from_slice(&fs::read(root.path().join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], 1);
    assert!(!run(root.path(), &plan, &["Cargo.toml", "--bin"])
        .status
        .success());
    fs::remove_file(root.path().join("Cargo.lock")).unwrap();
    assert!(!run(root.path(), &plan, &["Cargo.toml"]).status.success());
}
