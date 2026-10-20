//! Real compiler-to-SDK qualification; application fixtures contain no probes.
// Keep the shared helper formatted in its owning crate's Rust edition.
#[rustfmt::skip]
#[path = "../../../../crates/rust-adapter/tests/llvm/receiver.rs"]
mod receiver;
#[path = "support/capture.rs"]
mod capture;
use quux_otelc_config::common::{CommonConfig, Language};
use quux_otelc_rust_adapter::{compiler_frontend::Request, compiler_inventory::source_definitions};
use receiver::Receiver;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    time::{Duration, Instant},
};

struct Fixture {
    temporary: tempfile::TempDir,
    profile: Value,
    receiver: Receiver,
    plan: PathBuf,
    request: PathBuf,
    sources: BTreeMap<PathBuf, String>,
    driver: PathBuf,
}
fn sha(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
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
fn bounded(mut command: Command) -> Output {
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(30);
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            child.kill().unwrap();
            let output = child.wait_with_output().unwrap();
            panic!(
                "compiler/application deadline: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    child.wait_with_output().unwrap()
}
impl Fixture {
    fn new(source: &str, files: &[(&str, &str)]) -> Self {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("project");
        fs::create_dir(&root).unwrap();
        let root = root.canonicalize().unwrap();
        fs::write(root.join("app.rs"), source).unwrap();
        let mut sources = BTreeMap::from([(root.join("app.rs"), sha(&root.join("app.rs")))]);
        for (name, text) in files {
            let path = root.join(name);
            fs::write(&path, text).unwrap();
            let path = path.canonicalize().unwrap();
            sources.insert(path.clone(), sha(&path));
        }
        let checkout = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        let configured = std::env::var_os("OTELC_RUST_MIR_PROFILE");
        let descriptor = configured
            .clone()
            .map(PathBuf::from)
            .unwrap_or_else(|| checkout.join("target/debug/otelc-rust-compiler.json"));
        let profile: Value = serde_json::from_slice(
            &fs::read(descriptor)
                .expect("run ci/build_rust_mir.py after building the ordinary stable SDK"),
        )
        .unwrap();
        assert_eq!(
            profile["commit_hash"],
            "48a229ceaefd4985c50990b14116b6d856af0985"
        );
        for name in ["sdk", "rustc"] {
            assert_eq!(
                sha(Path::new(profile[name].as_str().unwrap())),
                profile[format!("{name}_sha256")]
            );
        }
        let driver = if configured.is_some() {
            let path = PathBuf::from(profile["driver"].as_str().unwrap());
            assert_eq!(sha(&path), profile["driver_sha256"]);
            path
        } else {
            PathBuf::from(env!("CARGO_BIN_EXE_otelc-rust-mir"))
        };
        let receiver = Receiver::new();
        let mut common = CommonConfig::from_text(&format!("schema_version=2\nlanguages=['rust']\n[sources]\ninclude=['**']\n[functions]\ninclude=['*.selected_*','app.*.alloc']\n[traces]\nenabled=true\nroot_sample_ratio=1.0\n[export]\nendpoint='{}'\ninterval_ms=60000\ntimeout_ms=1000\n", receiver.endpoint), false).unwrap();
        common.annotations.read_existing = true;
        common.functions.exclude = vec!["app.selected_excluded".into()];
        let policy = common.resolve(Language::Rust).unwrap();
        let runtime: quux_otelc_rust::policy::Plan =
            serde_json::from_value(serde_json::to_value(&policy).unwrap()).unwrap();
        let mut declarations = vec![];
        for path in sources.keys().filter(|path| path.starts_with(&root)) {
            let identity = path
                .strip_prefix(&root)
                .unwrap()
                .with_extension("")
                .to_string_lossy()
                .replace('/', ".");
            declarations.extend(
                source_definitions(
                    &fs::read_to_string(path).unwrap(),
                    path,
                    &identity,
                    &runtime,
                    true,
                    *path == root.join("app.rs"),
                )
                .unwrap(),
            );
        }
        let plan = temporary.path().join("plan.json");
        fs::write(&plan, serde_json::to_vec(&policy).unwrap()).unwrap();
        let request = temporary.path().join("request.json");
        fs::write(
            &request,
            serde_json::to_vec(&Request {
                schema_version: 1,
                root: root.clone(),
                entry: root.join("app.rs"),
                policy: runtime,
                sources: sources.clone(),
                declarations,
                protected_source_roots: serde_json::from_value(
                    profile["protected_source_roots"].clone(),
                )
                .unwrap(),
                discovery_argv: vec![
                    "app.rs".into(),
                    "--edition=2024".into(),
                    "-O".into(),
                    "-g".into(),
                ],
            })
            .unwrap(),
        )
        .unwrap();
        Self {
            temporary,
            profile,
            receiver,
            plan,
            request,
            sources,
            driver,
        }
    }
    fn root(&self) -> PathBuf {
        self.temporary
            .path()
            .join("project")
            .canonicalize()
            .unwrap()
    }
    fn command(&self, program: &str) -> Command {
        let mut command = Command::new(program);
        command.current_dir(self.root());
        for (name, _) in std::env::vars_os() {
            if name.to_string_lossy().starts_with("OTEL_")
                || name.to_string_lossy().starts_with("OTELC_")
            {
                command.env_remove(name);
            }
        }
        let library = Path::new(self.profile["rustc"].as_str().unwrap())
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("lib");
        command
            .env_remove("RUSTC_BOOTSTRAP")
            .env_remove("RUSTC_WRAPPER")
            .env_remove("RUSTC_WORKSPACE_WRAPPER")
            .env("DYLD_FALLBACK_LIBRARY_PATH", library)
            .env("OTELC_RUST_MIR_REQUEST", &self.request);
        command
    }
    fn compile(&self, instrumented: bool) -> (Output, PathBuf) {
        let program = if instrumented {
            self.driver.to_str().unwrap()
        } else {
            self.profile["rustc"].as_str().unwrap()
        };
        let binary = self.temporary.path().join(if instrumented {
            "instrumented"
        } else {
            "plain"
        });
        let mut command = self.command(program);
        command
            .args(["app.rs", "--edition=2024", "-O", "-g", "--extern"])
            .arg(format!(
                "quux_otelc_rust={}",
                self.profile["sdk"].as_str().unwrap()
            ))
            .arg("-L")
            .arg(format!(
                "dependency={}",
                Path::new(self.profile["sdk"].as_str().unwrap())
                    .parent()
                    .unwrap()
                    .join("deps")
                    .display()
            ))
            .arg("-o")
            .arg(&binary);
        (bounded(command), binary)
    }
    fn pair(&self, expected: u64) -> Vec<opentelemetry_proto::tonic::trace::v1::Span> {
        let capture = capture::Capture::new(
            self.temporary.path(),
            &self.sources,
            &self.plan,
            &self.request,
        );
        let mut runs = vec![];
        for measured in [false, true] {
            let (built, binary) = self.compile(measured);
            success(built);
            let mut command = self.command(binary.to_str().unwrap());
            command.env("OTELC_RUST_PLAN", &self.plan).env(
                "OTELC_REPORT_PATH",
                self.temporary.path().join("report.json"),
            );
            if let Some(capture) = &capture {
                capture.configure(&mut command, measured);
            }
            let output = bounded(command);
            if let Some(capture) = &capture {
                capture.record(measured, &binary, &output);
            }
            runs.push(success(output));
        }
        assert_eq!(runs[0].stdout, runs[1].stdout);
        assert_eq!(runs[0].stderr, runs[1].stderr);
        assert_eq!(runs[0].status.code(), runs[1].status.code());
        for (path, digest) in &self.sources {
            assert_eq!(&sha(path), digest);
        }
        let report: Value =
            serde_json::from_slice(&fs::read(self.temporary.path().join("report.json")).unwrap())
                .unwrap();
        assert_eq!(report["function_calls"], expected, "{report:#}");
        assert_eq!(report["export_loss"], 0);
        assert_eq!(report["export_finished"], true);
        assert!(
            report["losses"]
                .as_object()
                .unwrap()
                .values()
                .all(|value| value == 0)
        );
        assert_eq!(report["future_observers"]["pending"], 0);
        let spans = self.receiver.spans();
        assert_eq!(spans.len() as u64, expected);
        if let Some(capture) = capture {
            capture.complete(&report, expected);
        }
        spans
    }
}

#[test]
fn synchronous_locations_and_async_layout_cancellation_keep_original_behaviour() {
    let synchronous = Fixture::new(
        include_str!("fixtures/synchronous.rs"),
        &[("mir-module.rs", include_str!("fixtures/mir-module.rs"))],
    );
    let names: std::collections::BTreeSet<_> = synchronous
        .pair(6)
        .into_iter()
        .map(|span| span.name)
        .collect();
    assert_eq!(
        names,
        [
            "app.selected_root",
            "mir-module.selected_module",
            "app.selected_macro",
            "app.selected_loop",
            "app.selected_panics",
            "app.selected_caller"
        ]
        .map(String::from)
        .into_iter()
        .collect()
    );
    let asynchronous = Fixture::new(include_str!("fixtures/asynchronous.rs"), &[]);
    let spans = asynchronous.pair(4);
    assert_eq!(
        spans
            .iter()
            .filter(|span| span.parent_span_id.is_empty())
            .count(),
        3
    );
}

#[test]
fn global_allocator_locks_and_external_modules_cannot_reenter_selected_helpers() {
    Fixture::new(include_str!("fixtures/allocator.rs"), &[]).pair(1);
    let source = "use std::alloc::{GlobalAlloc, Layout, System};\n#[path=\"../allocator.rs\"] mod allocator;\n#[inline(never)] unsafe fn selected_allocate(layout: Layout) -> *mut u8 { unsafe { System.alloc(layout) } }\n#[inline(never)] fn selected_work() -> usize { std::hint::black_box(vec![1_u8;128]).len() }\nfn main() { println!(\"{}\", selected_work()); }\n";
    let allocator = include_str!("fixtures/allocator.rs")
        .split("#[inline(never)]")
        .next()
        .unwrap()
        .replace(
            "selected_allocate(layout)",
            "super::selected_allocate(layout)",
        );
    Fixture::new(source, &[("../allocator.rs", &allocator)]).pair(1);
}

#[test]
fn stable_features_mismatched_roots_changed_sources_and_bootstrap_fail_before_codegen() {
    let feature = Fixture::new("#![feature(test)]\nfn main() {}", &[]);
    for instrumented in [false, true] {
        let (output, binary) = feature.compile(instrumented);
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("E0554"));
        assert!(!binary.exists());
    }
    let fixture = Fixture::new("fn main() {}", &[("other.rs", "fn main() {}")]);
    let mut request: Value = serde_json::from_slice(&fs::read(&fixture.request).unwrap()).unwrap();
    request["entry"] = serde_json::json!(fixture.root().join("other.rs"));
    fs::write(&fixture.request, serde_json::to_vec(&request).unwrap()).unwrap();
    let (output, binary) = fixture.compile(true);
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("differs from its requested original crate root")
    );
    assert!(!binary.exists());
    request["entry"] = serde_json::json!(fixture.root().join("app.rs"));
    request["sources"][fixture.root().join("app.rs").to_str().unwrap()] =
        Value::String("0".repeat(64));
    fs::write(&fixture.request, serde_json::to_vec(&request).unwrap()).unwrap();
    let (output, binary) = fixture.compile(true);
    assert!(String::from_utf8_lossy(&output.stderr).contains("original Rust source differs"));
    assert!(!binary.exists());
    let mut command = fixture.command(fixture.driver.to_str().unwrap());
    command.arg("app.rs").env("RUSTC_BOOTSTRAP", "app");
    let output = bounded(command);
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("RUSTC_BOOTSTRAP to be unset"));
}

#[test]
fn similarly_named_application_allocator_traits_remain_instrumented() {
    let source = include_str!("fixtures/allocator.rs")
        .replace("use std::alloc::{GlobalAlloc, Layout, System};", "use std::alloc::{GlobalAlloc as RealAllocator, Layout, System};")
        .replace("unsafe impl GlobalAlloc for Allocator", "unsafe impl RealAllocator for Allocator")
        .replace("fn main() {", "mod ordinary { pub trait GlobalAlloc { fn alloc(&self) -> usize; } pub struct Value; impl GlobalAlloc for Value { fn alloc(&self) -> usize { 7 } } pub fn selected_fake() -> usize { GlobalAlloc::alloc(&Value) } } fn main() {")
        .replace("println!(\"{}\", selected_work());", "println!(\"{} {}\", selected_work(), ordinary::selected_fake());");
    let fixture = Fixture::new(&source, &[]);
    let spans = fixture.pair(3);
    assert!(spans.iter().any(|span| span.name.ends_with(".alloc")));
}

#[test]
fn async_unwind_preserves_payload_and_does_not_poison_the_next_poll_context() {
    let fixture = Fixture::new(include_str!("fixtures/async-unwind.rs"), &[]);
    let spans = fixture.pair(6);
    assert_eq!(
        spans
            .iter()
            .filter(|span| span.parent_span_id.is_empty())
            .count(),
        5
    );
    let panicking = spans
        .iter()
        .find(|span| span.name.ends_with(".selected_panicking"))
        .unwrap();
    assert_eq!(panicking.status.as_ref().unwrap().code, 2);
    let report: Value =
        serde_json::from_slice(&fs::read(fixture.temporary.path().join("report.json")).unwrap())
            .unwrap();
    assert_eq!(report["functions"]["app.selected_panicking"]["unwinds"], 1);
    assert_eq!(
        report["functions"]["app.selected_panicking"]["cancellations"],
        0
    );
}

#[test]
fn original_annotations_and_external_selection_share_exclusion_precedence() {
    let source = "fn selected_configured() -> u32 { column!() }\n// otelc.instrument\nfn tagged() -> u32 { column!() }\n// otelc.instrument\nfn selected_excluded() -> u32 { column!() }\nfn main() { println!(\"{} {} {}\", selected_configured(), tagged(), selected_excluded()); }\n";
    let fixture = Fixture::new(source, &[]);
    let names: Vec<_> = fixture.pair(2).into_iter().map(|span| span.name).collect();
    assert!(names.iter().any(|name| name == "app.selected_configured"));
    assert!(names.iter().any(|name| name == "app.tagged"));
    assert!(names.iter().all(|name| name != "app.selected_excluded"));
}

#[test]
fn cancellation_and_nested_body_panics_during_cleanup_keep_distinct_outcomes() {
    let fixture = Fixture::new(include_str!("fixtures/unwind-cleanup.rs"), &[]);
    let spans = fixture.pair(6);
    assert!(spans.iter().all(|span| span.parent_span_id.is_empty()));
    let report: Value =
        serde_json::from_slice(&fs::read(fixture.temporary.path().join("report.json")).unwrap())
            .unwrap();
    for name in [
        "selected_sync_panic",
        "selected_async_panic",
        "selected_drop_panic",
    ] {
        let name = format!("app.{name}");
        assert_eq!(report["functions"][&name]["count"], 1);
        assert_eq!(report["functions"][&name]["unwinds"], 1);
        assert_eq!(report["functions"][&name]["cancellations"], 0);
        let span = spans.iter().find(|span| span.name == name).unwrap();
        assert_eq!(span.status.as_ref().unwrap().code, 2);
    }
    assert_eq!(report["functions"]["app.selected_pending"]["unwinds"], 0);
    assert_eq!(
        report["functions"]["app.selected_pending"]["cancellations"],
        1
    );
    assert_eq!(report["functions"]["app.selected_ok"]["count"], 2);
    assert_eq!(report["functions"]["app.selected_ok"]["unwinds"], 0);
    assert_eq!(report["functions"]["app.selected_ok"]["cancellations"], 0);
}
