//! Qualified compiler profile: compare raw application channels and decoded SDK spans.
#[path = "llvm/receiver.rs"]
mod receiver;
use quux_otelc_config::common::{CommonConfig, Language};
use receiver::Receiver;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

fn adapter() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_otelc-rust-adapter"))
}
fn policy(root: &Path, receiver: &Receiver) -> PathBuf {
    policy_matching(root, receiver, vec!["*".into()])
}
fn policy_matching(root: &Path, receiver: &Receiver, include: Vec<String>) -> PathBuf {
    let text = format!("schema_version=2\nlanguages=['rust']\n[adapters.rust]\nbackend='llvm'\n[sources]\ninclude=['**']\n[functions]\ninclude=['*']\nexclude=['*.main', '*.main.*']\n[traces]\nenabled=true\nroot_sample_ratio=1.0\n[annotations]\nread_existing=true\n[resource]\nservice_name='rust-compiler-test'\n[export]\nendpoint='{}'\ninterval_ms=60000\ntimeout_ms=1000\n", receiver.endpoint);
    let mut common = CommonConfig::from_text(&text, false).unwrap();
    common.functions.include = include;
    let config = common.resolve(Language::Rust).unwrap();
    assert!(config.execution_available);
    let file = root.join("policy.json");
    fs::write(&file, serde_json::to_vec(&config).unwrap()).unwrap();
    file
}
fn command(binary: &Path, root: &Path) -> Command {
    let mut command = Command::new(binary);
    command
        .current_dir(root)
        .env_remove("RUSTC_WRAPPER")
        .env_remove("RUSTC_WORKSPACE_WRAPPER")
        .env_remove("OTELC_RUST_GENERATED")
        .env_remove("OTELC_RUSTC")
        .env("OTELC_FIXTURE_SENTINEL", "private-fixture-value")
        .env("OTELC_REPORT_PATH", root.join("report.json"));
    for (name, _) in std::env::vars_os() {
        if name.to_string_lossy().starts_with("OTEL_") {
            command.env_remove(name);
        }
    }
    command
}
fn run(root: &Path, plan: &Path, args: &[&str]) -> Output {
    command(&adapter(), root)
        .arg(plan)
        .args(args)
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
fn build(root: &Path, plan: &Path) -> Value {
    success(run(
        root,
        plan,
        &["--build", "app.rs", "--output-dir", "bundle"],
    ));
    let metadata: Value =
        serde_json::from_slice(&fs::read(root.join("bundle/build.json")).unwrap()).unwrap();
    assert_eq!(metadata["schema_version"], 2);
    assert_eq!(metadata["backend"], "llvm");
    let record = |name: &str, fingerprint: &str| {
        let bytes = fs::read(root.join("bundle").join(name)).unwrap();
        assert_eq!(
            metadata["compiler_hook"][fingerprint],
            format!("{:x}", Sha256::digest(&bytes))
        );
        serde_json::from_slice::<Value>(&bytes).unwrap()
    };
    let profile = record("profile.json", "profile_sha256");
    assert_eq!(profile["sdk_sha256"], metadata["sdk_sha256"]);
    assert_eq!(
        profile["plugin_sha256"],
        metadata["compiler_hook"]["plugin_sha256"]
    );
    let inventory = record("inventory.json", "inventory_sha256");
    assert_eq!(inventory["schema_version"], 2);
    let resolved = record("resolved.json", "resolved_sha256");
    assert_eq!(resolved["schema_version"], 1);
    assert_eq!(resolved["phase"], "llvm-pipeline-start");
    let functions = resolved["functions"].as_array().unwrap();
    assert_eq!(
        functions
            .iter()
            .filter(|function| function["startup"] == true)
            .count(),
        1
    );
    for function in functions {
        assert!(!function["compiled_symbol"].as_str().unwrap().is_empty());
        let file = function["file"].as_str().unwrap();
        assert_eq!(function["source_sha256"], inventory["sources"][file]);
        assert_eq!(
            function["source_sha256"],
            format!("{:x}", Sha256::digest(fs::read(file).unwrap()))
        );
        assert!(inventory["functions"]
            .as_array()
            .unwrap()
            .iter()
            .any(|requested| {
                requested["name"] == function["name"]
                    && requested["file"] == function["file"]
                    && requested["selected"] == function["selected"]
                    && requested["body_start_line"] == function["body_start_line"]
                    && requested["body_start_column"] == function["body_start_column"]
            }));
        for protected in profile["protected_source_roots"].as_array().unwrap() {
            assert!(
                !Path::new(file).starts_with(protected.as_str().unwrap()),
                "SDK implementation must never be probed"
            );
        }
    }
    metadata
}
fn compare(root: &Path, plan: &Path, metadata: &Value) -> Output {
    let compiler = metadata["compiler"]["program"].as_str().unwrap();
    let flags: Vec<_> = metadata["baseline_flags"]
        .as_array()
        .unwrap()
        .iter()
        .map(|value| value.as_str().unwrap())
        .collect();
    success(
        command(Path::new(compiler), root)
            .args(flags)
            .args(["app.rs", "-o", "plain"])
            .output()
            .unwrap(),
    );
    let plain = command(&root.join("plain"), root).output().unwrap();
    let measured = run(root, plan, &["--run", "bundle"]);
    assert_eq!(measured.status.code(), plain.status.code());
    assert_eq!(measured.stdout, plain.stdout);
    assert_eq!(measured.stderr, plain.stderr);
    measured
}
fn health(root: &Path, calls: u64) -> Value {
    let report: Value =
        serde_json::from_slice(&fs::read(root.join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], calls, "{report:#}");
    assert_eq!(report["export_finished"], true);
    assert_eq!(report["export_loss"], 0);
    assert!(report["losses"]
        .as_object()
        .unwrap()
        .values()
        .all(|value| value == 0));
    assert!(report["traces"]["losses"].as_object().unwrap().is_empty());
    report
}

#[test]
#[ignore = "requires the qualified nightly and make rust-llvm-build"]
fn compiler_preserves_macro_columns_tracked_callers_inline_bodies_and_closures() {
    let temporary = tempfile::Builder::new()
        .prefix("otelc LLVM #$ λ ")
        .tempdir()
        .unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy(root, &receiver);
    let source = r#"use std::column as alias;
macro_rules! forwarded { () => { column!() } }
#[inline(always)] fn direct()->u32 { column!() }
fn qualified()->u32 { std::column!() }
fn aliased()->u32 { alias!() }
fn indirect()->u32 { forwarded!() }
#[track_caller] fn caller()->&'static std::panic::Location<'static> { std::panic::Location::caller() }
fn closures()->(u32,u32) { let one=||column!(); let two=||std::column!(); (one(),two()) }
fn work()->(u32,u32,u32,u32,(u32,u32),&'static std::panic::Location<'static>) { (direct(),qualified(),aliased(),indirect(),closures(),caller()) }
fn main() { println!("{:?}",work()); }
fn unused() {}
"#;
    fs::write(root.join("app.rs"), source.replace('\n', "\r\n")).unwrap();
    let original = fs::read(root.join("app.rs")).unwrap();
    fs::write(root.join("unrelated.rs"), "this is deliberately not Rust").unwrap();
    let metadata = build(root, &plan);
    let inventory: Value =
        serde_json::from_slice(&fs::read(root.join("bundle/inventory.json")).unwrap()).unwrap();
    let resolved: Value =
        serde_json::from_slice(&fs::read(root.join("bundle/resolved.json")).unwrap()).unwrap();
    assert!(inventory["functions"]
        .as_array()
        .unwrap()
        .iter()
        .any(|function| function["name"] == "app.unused"));
    assert!(!resolved["functions"]
        .as_array()
        .unwrap()
        .iter()
        .any(|function| function["name"] == "app.unused"));
    success(compare(root, &plan, &metadata));
    health(root, 9);
    let spans = receiver.spans();
    assert_eq!(spans.len(), 9);
    let parent = spans.iter().find(|span| span.name == "app.work").unwrap();
    assert!(parent.parent_span_id.is_empty());
    assert!(spans.iter().all(|span| span.trace_id == parent.trace_id));
    assert_eq!(
        spans
            .iter()
            .filter(|span| span.name.contains("<closure>"))
            .count(),
        2
    );
    assert_eq!(fs::read(root.join("app.rs")).unwrap(), original);
}

#[test]
#[ignore = "requires the qualified nightly and make rust-llvm-build"]
fn compiler_retains_actual_module_inputs_and_runs_from_an_adapter_only_installation() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy(root, &receiver);
    fs::write(root.join("app.rs"), "#[path=\"module #$.rs\"] mod selected_module;\nfn parent()->u32{selected_module::selected()}\nfn main(){std::hint::black_box(env!(\"OTELC_FIXTURE_SENTINEL\"));println!(\"{}\",parent());}\n").unwrap();
    fs::write(
        root.join("module #$.rs"),
        "pub fn selected()->u32{column!()}\n",
    )
    .unwrap();
    let metadata = build(root, &plan);
    let measured = success(compare(root, &plan, &metadata));
    health(root, 2);
    assert_eq!(receiver.spans().len(), 2);
    let inventory = fs::read_to_string(root.join("bundle/inventory.json")).unwrap();
    assert!(!inventory.contains("private-fixture-value"));
    assert!(inventory.contains("module #$.rs"));
    let isolated = root.join("adapter-only");
    fs::create_dir(&isolated).unwrap();
    let copied = isolated.join("otelc-rust-adapter");
    fs::copy(adapter(), &copied).unwrap();
    fs::remove_file(root.join("app.rs")).unwrap();
    fs::remove_file(root.join("module #$.rs")).unwrap();
    let launched = success(
        command(&copied, root)
            .arg(&plan)
            .args(["--run", "bundle"])
            .env("OTELC_RUSTC", "/compiler-must-not-run")
            .output()
            .unwrap(),
    );
    assert_eq!(launched.stdout, measured.stdout);
    assert_eq!(launched.stderr, measured.stderr);
    for name in ["profile.json", "inventory.json", "resolved.json"] {
        let file = root.join("bundle").join(name);
        let original = fs::read(&file).unwrap();
        fs::write(&file, b"{}").unwrap();
        assert!(!run(root, &plan, &["--run", "bundle"]).status.success());
        fs::write(file, original).unwrap();
    }
}

#[test]
#[ignore = "requires the qualified nightly and make rust-llvm-build"]
fn compiler_unwind_cleanup_preserves_panic_payloads_locations_and_completed_trees() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy(root, &receiver);
    fs::write(root.join("app.rs"), "fn fail()->u32{std::panic::panic_any(String::from(\"payload\"));}\nfn selected()->bool{*std::panic::catch_unwind(fail).unwrap_err().downcast::<String>().unwrap()==\"payload\"}\nfn main(){std::panic::set_hook(Box::new(|info|{let at=info.location().unwrap();eprintln!(\"panic@{}:{}:{}\",at.file(),at.line(),at.column());}));println!(\"{}\",selected());}\n").unwrap();
    let metadata = build(root, &plan);
    success(compare(root, &plan, &metadata));
    let report = health(root, 2);
    assert_eq!(report["functions"]["app.fail"]["unwinds"], 1);
    let spans = receiver.spans();
    assert_eq!(spans.len(), 2);
    let parent = spans
        .iter()
        .find(|span| span.name == "app.selected")
        .unwrap();
    let child = spans.iter().find(|span| span.name == "app.fail").unwrap();
    assert_eq!(child.parent_span_id, parent.span_id);
    assert_eq!(child.status.as_ref().unwrap().code, 2);
}

#[test]
#[ignore = "requires the qualified nightly and make rust-llvm-build"]
fn unqualified_async_and_cargo_are_rejected_before_application_execution() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy(root, &receiver);
    fs::write(
        root.join("app.rs"),
        "async fn selected(){} fn main(){std::fs::write(\"ran\",\"yes\").unwrap();}\n",
    )
    .unwrap();
    let output = run(root, &plan, &["app.rs"]);
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("future-completion"));
    assert!(!root.join("ran").exists());
    assert!(!run(root, &plan, &["Cargo.toml"]).status.success());
    assert!(receiver.spans().is_empty());
}

#[test]
#[ignore = "requires the qualified nightly and make rust-llvm-build"]
fn existing_annotations_select_original_declarations_and_exclusions_win() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy_matching(
        root,
        &receiver,
        vec!["app.selected".into(), "app.excluded".into()],
    );
    // Existing annotations are comments in ordinary source, not required edits.
    let source = "// otelc.instrument\nfn tagged()->u32{column!()}\n// otelc.exclude\nfn excluded()->u32{column!()}\nfn selected()->(u32,u32){(tagged(),excluded())}\nfn main(){println!(\"{:?}\",selected());}\n";
    fs::write(root.join("app.rs"), source).unwrap();
    let metadata = build(root, &plan);
    success(compare(root, &plan, &metadata));
    health(root, 2);
    let spans = receiver.spans();
    assert_eq!(spans.len(), 2);
    assert!(spans.iter().any(|span| span.name == "app.tagged"));
    assert!(!spans.iter().any(|span| span.name == "app.excluded"));
    assert_eq!(fs::read_to_string(root.join("app.rs")).unwrap(), source);
}

#[test]
#[ignore = "requires the qualified nightly and make rust-llvm-build"]
fn nested_same_line_functions_closures_and_specialised_methods_keep_distinct_identities() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy(root, &receiver);
    let source = "struct Value<T>(T); impl Value<u32>{fn chosen(&self)->u32{column!()}} impl Value<u64>{fn chosen(&self)->u32{column!()}} fn outer()->(u32,u32,u32,u32){fn outer()->u32{column!()} let λ=|| || column!(); let child=λ(); (outer(),child(),Value(1u32).chosen(),Value(1u64).chosen())} fn main(){println!(\"{:?}\",outer());}\n";
    fs::write(root.join("app.rs"), source).unwrap();
    let metadata = build(root, &plan);
    success(compare(root, &plan, &metadata));
    let report = health(root, 6);
    let names = report["functions"].as_object().unwrap();
    assert_eq!(
        names.len(),
        6,
        "same-line probes must not be attributed to another declaration"
    );
    assert!(names.values().all(|value| value["count"] == 1));
    assert!(names.contains_key("app.outer"));
    assert!(names.contains_key("app.outer.outer"));
    assert_eq!(
        names
            .keys()
            .filter(|name| name.contains("<closure>"))
            .count(),
        2
    );
    let spans = receiver.spans();
    assert_eq!(spans.len(), 6);
    let parent = spans.iter().find(|span| span.name == "app.outer").unwrap();
    assert!(parent.parent_span_id.is_empty());
    assert!(spans.iter().all(|span| span.trace_id == parent.trace_id));
}
