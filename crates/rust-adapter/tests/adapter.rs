use quux_otelc_config::common::{CommonConfig, Language};
use quux_otelc_rust::policy::Plan;
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
    // This fixture checks source/behaviour preservation, not scheduling latency.
    // Keep the normal shutdown budget under coverage on shared CI runners.
    value["runtime"]["shutdown_timeout_ms"] = serde_json::json!(2000);
    value["metrics_endpoint"] = serde_json::json!("http://127.0.0.1:9/v1/metrics");
    let path = root.join("plan.json");
    fs::write(&path, value.to_string()).unwrap();
    path
}
#[test]
fn closure_runtime_preserves_traits_borrows_capture_precision_drops_and_panic_payloads() {
    let root = tempfile::tempdir().unwrap();
    let source = include_str!("../../../examples/apps/rust_closure_app.rs");
    fs::write(root.path().join("main.rs"), source).unwrap();
    success(
        Command::new("rustc")
            .args(["--edition=2024", "-O", "-g", "main.rs", "-o", "plain"])
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    let baseline = success(Command::new(root.path().join("plain")).output().unwrap());
    let path = write_plan(root.path());
    let mut plan: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    plan["functions"]["include"] = serde_json::json!(["*.<closure>@*"]);
    fs::write(&path, plan.to_string()).unwrap();
    let instrumented = success(run(root.path(), &path, &["main.rs"]));
    assert_eq!(baseline.stdout, instrumented.stdout);
    assert_eq!(baseline.stderr, instrumented.stderr);
    assert_eq!(
        fs::read_to_string(root.path().join("main.rs")).unwrap(),
        source
    );
    let report: serde_json::Value =
        serde_json::from_slice(&fs::read(root.path().join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], 20);
    assert!(report["functions"]
        .as_object()
        .unwrap()
        .values()
        .all(|value| value["cancellations"] == 0));
    assert_eq!(
        report["functions"]
            .as_object()
            .unwrap()
            .values()
            .map(|value| value["unwinds"].as_u64().unwrap())
            .sum::<u64>(),
        1
    );
    assert!(report["losses"]
        .as_object()
        .unwrap()
        .values()
        .all(|count| count == 0));
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
#[test]
fn compact_async_tails_keep_nested_delimiters_coercions_and_empty_bodies_valid() {
    let source = include_str!("../../../examples/apps/rust_async_compact.rs");
    let mut plan = policy();
    plan.functions.include = vec!["main.*".into()];
    plan.functions.exclude = vec!["*.main".into(), "*.drive".into(), "*.double".into()];
    let root = tempfile::tempdir().unwrap();
    let input = root.path().join("main.rs");
    fs::write(&input, source).unwrap();
    let path = write_plan(root.path());
    let mut value: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    value["functions"]["include"] = serde_json::json!(plan.functions.include);
    value["functions"]["exclude"] = serde_json::json!(plan.functions.exclude);
    fs::write(&path, value.to_string()).unwrap();
    success(
        Command::new("rustc")
            .args(["--edition=2024", "main.rs", "-o", "plain"])
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    let baseline = success(Command::new(root.path().join("plain")).output().unwrap());
    let instrumented = success(run(root.path(), &path, &["main.rs"]));
    assert_eq!(baseline.stdout, instrumented.stdout);
    assert_eq!(baseline.stderr, instrumented.stderr);
    assert_eq!(fs::read_to_string(input).unwrap(), source);
    let report: serde_json::Value =
        serde_json::from_slice(&fs::read(root.path().join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], 7);
}

#[test]
fn linked_rust_modules_are_instrumented_without_changing_sources_or_permissions() {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let root = tempfile::tempdir().unwrap();
    let external = tempfile::tempdir().unwrap();
    let plan = write_plan(root.path());
    let original = external.path().join("leaf.rs");
    let module = "pub fn value() -> i32 { 42 }\n";
    fs::write(&original, module).unwrap();
    fs::set_permissions(&original, fs::Permissions::from_mode(0o400)).unwrap();
    symlink(&original, root.path().join("linked.rs")).unwrap();
    symlink(external.path(), root.path().join("src")).unwrap();
    let main = "mod linked; #[path=\"src/leaf.rs\"] mod nested; fn main() { let result=linked::value()+nested::value(); assert_eq!(result,84); println!(\"result={result}\"); }";
    fs::write(root.path().join("main.rs"), main).unwrap();
    success(
        Command::new("rustc")
            .args(["--edition=2024", "main.rs", "-o", "plain"])
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    let plain = success(Command::new(root.path().join("plain")).output().unwrap());
    let instrumented = success(run(root.path(), &plan, &["main.rs"]));
    assert_eq!(instrumented.stdout, plain.stdout);
    assert_eq!(instrumented.stderr, plain.stderr);
    let report: serde_json::Value =
        serde_json::from_slice(&fs::read(root.path().join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], 2);
    assert_eq!(report["functions"]["linked.value"]["count"], 1);
    assert_eq!(report["functions"]["src.leaf.value"]["count"], 1);
    assert_eq!(fs::read_to_string(&original).unwrap(), module);
    assert_eq!(
        fs::read_to_string(root.path().join("main.rs")).unwrap(),
        main
    );
    assert!(root.path().join("linked.rs").is_symlink());
    assert!(root.path().join("src").is_symlink());
    assert_eq!(
        fs::metadata(&original).unwrap().permissions().mode() & 0o777,
        0o400
    );
}
