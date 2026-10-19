//! Compiler diagnostics belong to the build result, independently of application channels.
use quux_otelc_config::common::{CommonConfig, Language};
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

fn policy(root: &Path) -> std::path::PathBuf {
    let config=CommonConfig::from_text("schema_version=2\nlanguages=['rust']\n[sources]\ninclude=['*.rs']\n[functions]\ninclude=['*']\nexclude=['*.main']\n[metrics]\nenabled=false\n[export]\nendpoint='http://127.0.0.1:9'\ntimeout_ms=40\ninterval_ms=60000\n",false).unwrap();
    let file = root.join("policy.json");
    fs::write(
        &file,
        serde_json::to_vec(&config.resolve(Language::Rust).unwrap()).unwrap(),
    )
    .unwrap();
    file
}
fn run(root: &Path, plan: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_otelc-rust-adapter"))
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
fn success(output: Output) -> Output {
    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    output
}

#[test]
fn build_retains_warnings_without_running_and_repeated_launches_do_not_compile() {
    let temporary = tempfile::Builder::new()
        .prefix("otelc artifact tests ")
        .tempdir()
        .unwrap();
    let root = temporary.path();
    let plan = policy(root);
    let source="fn selected(value:u128)->u128{value}\nfn unused(value:f32)->f32{value}\nfn main(){std::fs::write(\"application-ran\",\"yes\").unwrap();println!(\"result={}\",selected(u128::MAX));eprintln!(\"application stderr\");}\n";
    fs::write(root.join("app.rs"), source).unwrap();
    let original_plan = fs::read(&plan).unwrap();
    let built = success(run(
        root,
        &plan,
        &["--build", "app.rs", "--output-dir", "private bundle"],
    ));
    assert!(built.stdout.is_empty());
    assert!(String::from_utf8_lossy(&built.stderr).contains("function `unused` is never used"));
    assert!(!root.join("application-ran").exists());
    let bundle = root.join("private bundle");
    let manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(bundle.join("build.json")).unwrap()).unwrap();
    assert_eq!(
        manifest["baseline_flags"],
        serde_json::json!(["--edition=2024", "-O", "-g"])
    );
    let args = manifest["compiler"]["argv"].as_array().unwrap();
    assert!(args.iter().any(|arg| arg == "--edition=2024"));
    assert!(args.iter().any(|arg| arg == "-O"));
    assert!(args.iter().any(|arg| arg == "-g"));
    for field in [
        "executable_sha256",
        "plan_sha256",
        "source_sha256",
        "sdk_sha256",
        "adapter_sha256",
    ] {
        assert_eq!(manifest[field].as_str().unwrap().len(), 64, "{field}");
    }
    let plain_build = Command::new("rustc")
        .current_dir(root)
        .args(["app.rs", "--edition=2024", "-O", "-g", "-o", "plain"])
        .output()
        .unwrap();
    assert!(plain_build.status.success());
    assert!(!plain_build.stderr.is_empty());
    let plain = success(
        Command::new(root.join("plain"))
            .current_dir(root)
            .output()
            .unwrap(),
    );
    let isolated = root.join("adapter-only");
    fs::create_dir(&isolated).unwrap();
    let copied = isolated.join("otelc-rust-adapter");
    fs::copy(env!("CARGO_BIN_EXE_otelc-rust-adapter"), &copied).unwrap();
    fs::remove_file(root.join("app.rs")).unwrap();
    for _ in 0..3 {
        let measured = success(
            Command::new(&copied)
                .arg(&plan)
                .args(["--run", "private bundle"])
                .current_dir(root)
                .env("OTELC_RUSTC", root.join("compiler-must-not-run"))
                .env("OTELC_REPORT_PATH", root.join("report.json"))
                .output()
                .unwrap(),
        );
        assert_eq!(measured.stdout, plain.stdout);
        assert_eq!(measured.stderr, plain.stderr);
        assert_eq!(fs::read(root.join("application-ran")).unwrap(), b"yes");
    }
    assert_eq!(fs::read(&plan).unwrap(), original_plan);
    fs::write(root.join("app.rs"), source).unwrap();
    let refused = run(
        root,
        &plan,
        &["--build", "app.rs", "--output-dir", "private bundle"],
    );
    assert!(!refused.status.success());
    assert_eq!(fs::read_to_string(root.join("app.rs")).unwrap(), source);
}

#[test]
fn changed_binary_plan_adapter_and_selection_are_rejected_before_execution() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let plan = policy(root);
    fs::write(
        root.join("app.rs"),
        "fn selected(){} fn main(){selected();std::fs::write(\"ran\",\"yes\").unwrap();}\n",
    )
    .unwrap();
    success(run(
        root,
        &plan,
        &["--build", "app.rs", "--output-dir", "bundle"],
    ));
    let executable = root.join("bundle/application");
    let binary = fs::read(&executable).unwrap();
    fs::write(&executable, b"tampered").unwrap();
    assert!(!run(root, &plan, &["--run", "bundle"]).status.success());
    fs::write(&executable, &binary).unwrap();
    let stored = root.join("bundle/plan.json");
    let stored_plan = fs::read(&stored).unwrap();
    fs::write(&stored, b"{}").unwrap();
    assert!(!run(root, &plan, &["--run", "bundle"]).status.success());
    fs::write(&stored, &stored_plan).unwrap();
    let metadata = root.join("bundle/build.json");
    let original = fs::read(&metadata).unwrap();
    let mut value: serde_json::Value = serde_json::from_slice(&original).unwrap();
    value["adapter_sha256"] = serde_json::json!("0".repeat(64));
    fs::write(&metadata, value.to_string()).unwrap();
    assert!(!run(root, &plan, &["--run", "bundle"]).status.success());
    fs::write(&metadata, original).unwrap();
    let mut current: serde_json::Value = serde_json::from_slice(&fs::read(&plan).unwrap()).unwrap();
    current["functions"]["include"] = serde_json::json!(["different"]);
    fs::write(&plan, current.to_string()).unwrap();
    assert!(!run(root, &plan, &["--run", "bundle"]).status.success());
    assert!(!root.join("ran").exists());
}

#[test]
fn failed_compilation_and_unsupported_build_options_publish_no_bundle() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let plan = policy(root);
    fs::write(root.join("broken.rs"), "fn main(){missing_function();}\n").unwrap();
    for args in [
        vec!["--build", "broken.rs", "--output-dir", "bundle"],
        vec!["--build", "Cargo.toml", "--output-dir", "bundle"],
        vec!["--build", "broken.rs"],
        vec!["--run"],
    ] {
        assert!(!run(root, &plan, &args).status.success());
        assert!(!root.join("bundle").exists());
    }
}

#[test]
fn runtime_metrics_and_trace_policy_can_change_without_rebuilding() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let plan = policy(root);
    fs::write(
        root.join("app.rs"),
        "fn selected()->u32{7} fn main(){println!(\"{}\",selected());}\n",
    )
    .unwrap();
    success(run(
        root,
        &plan,
        &["--build", "app.rs", "--output-dir", "bundle"],
    ));
    let original = fs::read(&plan).unwrap();
    let mut current: serde_json::Value = serde_json::from_slice(&original).unwrap();
    current["metrics"]["enabled"] = serde_json::json!(true);
    current["resource"]["service_name"] = serde_json::json!("artifact-runtime-policy");
    current["traces"] = serde_json::json!({"enabled":true,"root_sample_ratio":1.0,"max_active_traces":8,"max_spans_per_trace":32});
    current["trace_export"] = serde_json::json!({"endpoint":"http://127.0.0.1:9/v1/traces","protocol":"http/protobuf","timeout_ms":40});
    let runtime_policy = root.join("runtime-policy.json");
    fs::write(&runtime_policy, current.to_string()).unwrap();
    let output = success(run(root, &runtime_policy, &["--run", "bundle"]));
    assert_eq!(output.stdout, b"7\n");
    assert!(output.stderr.is_empty());
    let report: serde_json::Value =
        serde_json::from_slice(&fs::read(root.join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], 1);
    assert_eq!(report["functions"]["app.selected"]["count"], 1);
    assert_eq!(report["traces"]["completed_trees"], 1);
    assert_eq!(fs::read(&plan).unwrap(), original);
    assert_eq!(fs::read(root.join("bundle/plan.json")).unwrap(), original);
}
