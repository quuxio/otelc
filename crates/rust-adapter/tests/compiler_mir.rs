//! Exercise the public compiler path on original sources, not private requests.
#[path = "llvm/receiver.rs"]
mod receiver;
use quux_otelc_config::common::{CommonConfig, Language};
use receiver::Receiver;
use serde_json::{json, Value};
use std::{
    fs,
    io::Read,
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

fn bounded(command: &mut Command, timeout: Duration) -> (Output, bool) {
    let mut child = command
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let (sender, receiver) = mpsc::channel();
    let read = |mut stream: Box<dyn Read + Send>, standard_output| {
        let sender = sender.clone();
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let result = stream.read_to_end(&mut bytes).map(|_| bytes);
            let _ = sender.send((standard_output, result));
        });
    };
    read(Box::new(stdout), true);
    read(Box::new(stderr), false);
    drop(sender);
    let mut stdout = None;
    let mut stderr = None;
    let deadline = Instant::now() + timeout;
    let mut expired = false;
    loop {
        while let Ok((standard_output, bytes)) = receiver.try_recv() {
            *if standard_output {
                &mut stdout
            } else {
                &mut stderr
            } = Some(bytes.unwrap());
        }
        if child.try_wait().unwrap().is_some() && stdout.is_some() && stderr.is_some() {
            break;
        }
        if Instant::now() >= deadline {
            // Only this invocation and its descendants occupy the newly
            // created group. Kill them together so inherited pipes close too.
            let killed = Command::new("/bin/kill")
                .arg("-KILL")
                .arg(format!("-{}", child.id()))
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .unwrap();
            assert!(killed.success() || child.try_wait().unwrap().is_some());
            expired = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    let status = child.wait().unwrap();
    let drain_deadline = Instant::now() + Duration::from_secs(5);
    while stdout.is_none() || stderr.is_none() {
        let (standard_output, bytes) = receiver
            .recv_timeout(drain_deadline.saturating_duration_since(Instant::now()))
            .expect("invocation output pipes did not close after group termination");
        *if standard_output {
            &mut stdout
        } else {
            &mut stderr
        } = Some(bytes.unwrap());
    }
    (
        Output {
            status,
            stdout: stdout.unwrap(),
            stderr: stderr.unwrap(),
        },
        expired,
    )
}

trait BoundedOutput {
    fn bounded_output(&mut self) -> Output;
}
impl BoundedOutput for Command {
    fn bounded_output(&mut self) -> Output {
        let (output, expired) = bounded(self, Duration::from_secs(90));
        assert!(
            !expired,
            "compiler/application deadline: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        output
    }
}

#[test]
fn deadline_reaps_the_invocation_and_descendants_holding_output_pipes() {
    let started = Instant::now();
    let (output, expired) = bounded(
        Command::new("/bin/sh").args(["-c", "sleep 30 & printf 'started\\n'; wait"]),
        Duration::from_millis(150),
    );
    assert!(expired);
    assert!(!output.status.success());
    assert_eq!(output.stdout, b"started\n");
    assert!(started.elapsed() < Duration::from_secs(5));
}

#[test]
fn deadline_includes_inherited_pipes_after_the_parent_exits() {
    let started = Instant::now();
    let (output, expired) = bounded(
        Command::new("/bin/sh").args(["-c", "sleep 2 & printf 'started\\n'; exit 0"]),
        Duration::from_millis(150),
    );
    assert!(expired);
    assert!(output.status.success());
    assert_eq!(output.stdout, b"started\n");
    assert!(started.elapsed() < Duration::from_secs(5));
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

fn policy(root: &Path, receiver: &Receiver) -> PathBuf {
    let common = CommonConfig::from_text(&format!("schema_version=2\nlanguages=['rust']\n[sources]\ninclude=['**']\n[functions]\ninclude=['*.selected_*']\nexclude=['*.selected_excluded']\n[annotations]\nread_existing=true\n[traces]\nenabled=true\nroot_sample_ratio=1.0\n[export]\nendpoint='{}'\ninterval_ms=60000\ntimeout_ms=1000\n",receiver.endpoint),false).unwrap();
    let path = root.join("policy.json");
    fs::write(
        &path,
        serde_json::to_vec(&common.resolve(Language::Rust).unwrap()).unwrap(),
    )
    .unwrap();
    path
}

fn command(root: &Path, plan: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_otelc-rust-adapter"));
    command
        .arg(plan)
        .current_dir(root)
        .env_remove("RUSTC_BOOTSTRAP")
        .env_remove("RUSTC_WRAPPER")
        .env_remove("RUSTC_WORKSPACE_WRAPPER")
        .env_remove("OTELC_RUST_ROOT")
        .env_remove("OTELC_RUST_GENERATED")
        .env_remove("OTELC_RUSTC")
        .env("OTELC_REPORT_PATH", root.join("report.json"));
    for (name, _) in std::env::vars_os() {
        if name.to_string_lossy().starts_with("OTEL_") {
            command.env_remove(name);
        }
    }
    command
}

fn pair(
    source: &str,
    files: &[(&str, &str)],
    expected: u64,
) -> Vec<opentelemetry_proto::tonic::trace::v1::Span> {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy(root, &receiver);
    fs::write(root.join("app.rs"), source).unwrap();
    for (name, text) in files {
        fs::write(root.join(name), text).unwrap();
    }
    success(
        Command::new("rustc")
            .current_dir(root)
            .args(["app.rs", "--edition=2024", "-O", "-g", "-o", "plain"])
            .bounded_output(),
    );
    let plain = success(
        Command::new(root.join("plain"))
            .current_dir(root)
            .bounded_output(),
    );
    let measured = success(command(root, &plan).arg("app.rs").bounded_output());
    assert_eq!(plain.stdout, measured.stdout);
    assert_eq!(plain.stderr, measured.stderr);
    assert_eq!(plain.status.code(), measured.status.code());
    assert_eq!(fs::read_to_string(root.join("app.rs")).unwrap(), source);
    for (name, text) in files {
        assert_eq!(fs::read_to_string(root.join(name)).unwrap(), *text);
    }
    let report: Value =
        serde_json::from_slice(&fs::read(root.join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], expected);
    assert_eq!(report["traces"]["losses"], json!({}));
    let spans = receiver.spans();
    assert_eq!(spans.len() as u64, expected);
    spans
}

#[test]
fn original_columns_tracked_callers_unicode_and_crlf_are_preserved() {
    let source="// Unicode λ\r\n#[track_caller]\r\nfn selected()->(u32,u32){(column!(),std::panic::Location::caller().column())}\r\nfn main(){println!(\"{:?}\",selected());}\r\n";
    // Use the same public selection mechanism as the other original fixtures.
    let source = source.replace("selected()", "selected_columns()");
    let spans = pair(&source, &[], 1);
    assert_eq!(spans[0].name, "app.selected_columns");
}

#[test]
fn inactive_async_entry_does_not_reject_the_compilers_actual_main() {
    let source = "#[cfg(any())]\nasync fn main(){}\nfn selected_location()->u32{column!()}\nfn main(){println!(\"{}\",selected_location());}\n";
    let spans = pair(source, &[], 1);
    assert_eq!(spans[0].name, "app.selected_location");
}

#[test]
fn legacy_marker_text_is_ordinary_original_source() {
    let source = "fn selected_marker()->(&'static str,u32){(\"/*quux.otelc.generated*/\",column!())}\nfn main(){println!(\"{:?}\",selected_marker());}\n";
    let spans = pair(source, &[], 1);
    assert_eq!(spans[0].name, "app.selected_marker");
}

#[test]
fn location_macros_inline_bodies_and_expanded_callbacks_keep_original_observations() {
    let source = r#"// Unicode λ
use std::column as alias;
macro_rules! forwarded { () => { column!() } }
#[inline(always)] fn selected_direct()->u32 { column!() }
fn selected_qualified()->u32 { std::column!() }
fn selected_aliased()->u32 { alias!() }
fn selected_indirect()->u32 { forwarded!() }
#[track_caller] fn selected_caller()->&'static std::panic::Location<'static> { std::panic::Location::caller() }
fn selected_closures()->(u32,u32) { let one=||column!(); let two=||std::column!(); (one(),two()) }
fn selected_unicode()->(u32,u32) { let λ=7; (λ,column!()) }
fn selected_work()->(u32,u32,u32,u32,(u32,u32),&'static std::panic::Location<'static>,u32,(u32,u32)) { fn selected_nested()->u32{column!()} (selected_direct(),selected_qualified(),selected_aliased(),selected_indirect(),selected_closures(),selected_caller(),selected_nested(),selected_unicode()) }
fn main() { println!("{:?}",selected_work()); }
"#;
    let spans = pair(&source.replace('\n', "\r\n"), &[], 11);
    assert_eq!(
        spans
            .iter()
            .filter(|span| span.name.contains("<closure"))
            .count(),
        2
    );
    let parent = spans
        .iter()
        .find(|span| span.name == "app.selected_work")
        .unwrap();
    assert!(parent.parent_span_id.is_empty());
    assert!(spans.iter().all(|span| span.trace_id == parent.trace_id));
    let expanded = "macro_rules! callback{()=>{|| eprintln!(\"generated\")}}\nfn selected_work(){let ordinary=|| eprintln!(\"ordinary\");let generated=callback!();ordinary();generated();}\nfn main(){selected_work();}\n";
    let spans = pair(expanded, &[], 3);
    assert_eq!(
        spans
            .iter()
            .filter(|span| span.name.contains("<closure"))
            .count(),
        2
    );
}

#[test]
fn public_async_layout_send_cancellation_panics_and_allocator_pairs_keep_behaviour() {
    for (source, expected) in [
        (
            include_str!("../../../native/rust/mir/tests/fixtures/asynchronous.rs"),
            4,
        ),
        (
            include_str!("../../../native/rust/mir/tests/fixtures/async-unwind.rs"),
            6,
        ),
        (
            include_str!("../../../native/rust/mir/tests/fixtures/unwind-cleanup.rs"),
            6,
        ),
        (
            include_str!("../../../native/rust/mir/tests/fixtures/allocator.rs"),
            1,
        ),
    ] {
        pair(source, &[], expected);
    }
}

#[test]
fn retained_compiler_inventory_and_replay_do_not_execute_or_reopen_build_inputs() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy(root, &receiver);
    let source="macro_rules! declare{()=>{fn selected_macro<T:Copy>(value:T)->(T,u32){(value,column!())}}} declare!();\nfn unused<T>(value:T)->T{value}\nfn main(){std::fs::write(\"ran\",\"yes\").unwrap();println!(\"{:?}\",selected_macro(std::hint::black_box(7)));}\n";
    fs::write(root.join("app.rs"), source).unwrap();
    success(
        command(root, &plan)
            .args(["--build", "app.rs", "--output-dir", "bundle"])
            .bounded_output(),
    );
    let metadata: Value =
        serde_json::from_slice(&fs::read(root.join("bundle/build.json")).unwrap()).unwrap();
    assert_eq!(metadata["schema_version"], 3);
    assert_eq!(metadata["backend"], "compiler");
    assert!(metadata.get("compiler_hook").is_none());
    assert_eq!(
        metadata["compiler_driver"]["driver_sha256"]
            .as_str()
            .unwrap()
            .len(),
        64
    );
    let inspected: Value = serde_json::from_slice(
        &success(
            command(root, &plan)
                .args(["--inspect", "app.rs", "--json"])
                .bounded_output(),
        )
        .stdout,
    )
    .unwrap();
    let inventory: Value =
        serde_json::from_slice(&fs::read(root.join("bundle/inventory.json")).unwrap()).unwrap();
    assert_eq!(inspected["backend"], "compiler");
    assert_eq!(inspected["phase"], "compiled");
    assert_eq!(inspected["functions"], inventory["functions"]);
    assert!(inventory["functions"]
        .as_array()
        .unwrap()
        .iter()
        .any(|row| row["name"] == "app.selected_macro"
            && row["selected"] == true
            && row["emitted"] == true));
    assert!(!root.join("ran").exists());
    assert!(!root.join("report.json").exists());
    assert!(receiver.spans().is_empty());
    for name in ["profile.json", "inventory.json", "compiler-request.json"] {
        let path = root.join("bundle").join(name);
        let original = fs::read(&path).unwrap();
        let mut altered = original.clone();
        altered.push(b' ');
        fs::write(&path, altered).unwrap();
        let rejected = command(root, &plan)
            .args(["--run", "bundle"])
            .bounded_output();
        assert!(!rejected.status.success(), "accepted changed {name}");
        assert!(!root.join("ran").exists());
        fs::write(path, original).unwrap();
    }
    let metadata_path = root.join("bundle/build.json");
    let original_metadata = fs::read(&metadata_path).unwrap();
    for change in ["driver_sha256", "missing_driver", "schema_version"] {
        let mut altered: Value = serde_json::from_slice(&original_metadata).unwrap();
        match change {
            "driver_sha256" => altered["compiler_driver"]["driver_sha256"] = "0".repeat(64).into(),
            "missing_driver" => {
                altered.as_object_mut().unwrap().remove("compiler_driver");
            }
            _ => altered["schema_version"] = json!(1),
        }
        fs::write(&metadata_path, serde_json::to_vec(&altered).unwrap()).unwrap();
        assert!(!command(root, &plan)
            .args(["--run", "bundle"])
            .bounded_output()
            .status
            .success());
        assert!(!root.join("ran").exists());
    }
    fs::write(metadata_path, original_metadata).unwrap();
    let copied = root.join("isolated/otelc-rust-adapter");
    fs::create_dir(copied.parent().unwrap()).unwrap();
    fs::copy(env!("CARGO_BIN_EXE_otelc-rust-adapter"), &copied).unwrap();
    fs::remove_file(root.join("app.rs")).unwrap();
    let output = success(
        Command::new(copied)
            .arg(&plan)
            .args(["--run", "bundle"])
            .current_dir(root)
            .env("OTELC_RUSTC", root.join("must-not-run"))
            .env("OTELC_REPORT_PATH", root.join("report.json"))
            .bounded_output(),
    );
    assert!(!output.stdout.is_empty());
    assert_eq!(fs::read(root.join("ran")).unwrap(), b"yes");
    assert_eq!(receiver.spans().len(), 1);
}

#[test]
fn cargo_preserves_original_columns_dependencies_lockfile_and_application_arguments() {
    let temporary = tempfile::Builder::new()
        .prefix("original Cargo columns ")
        .tempdir()
        .unwrap();
    let root = temporary.path();
    let receiver = Receiver::new();
    let plan = policy(root, &receiver);
    for directory in ["src", "helper/src"] {
        fs::create_dir_all(root.join(directory)).unwrap();
    }
    let files = [
        ("Cargo.toml", "[package]\nname='original-source-pair'\nversion='0.1.0'\nedition='2024'\n[workspace]\n[dependencies]\nhelper={path='helper'}\n"),
        ("helper/Cargo.toml", "[package]\nname='helper'\nversion='0.1.0'\nedition='2024'\n"),
        ("helper/src/lib.rs", "#[track_caller]\npub fn selected_helper()->(u32,u32){(column!(),std::panic::Location::caller().column())}\n"),
        ("src/main.rs", "// Unicode λ\r\n#[track_caller]\r\nfn selected_columns()->(u32,u32){(column!(),std::panic::Location::caller().column())}\r\nfn main(){println!(\"{:?} {:?} {:?}\",selected_columns(),helper::selected_helper(),std::env::args().skip(1).collect::<Vec<_>>());eprintln!(\"application stderr\");}\r\n"),
    ];
    for (name, text) in files {
        fs::write(root.join(name), text).unwrap();
    }
    success(
        Command::new("cargo")
            .current_dir(root)
            .args(["generate-lockfile", "--offline"])
            .bounded_output(),
    );
    let lock = fs::read(root.join("Cargo.lock")).unwrap();
    success(
        Command::new("cargo")
            .current_dir(root)
            .args(["build", "--locked", "--offline"])
            .arg("--target-dir")
            .arg(root.join("plain-target"))
            .bounded_output(),
    );
    let plain = success(
        Command::new(root.join("plain-target/debug/original-source-pair"))
            .current_dir(root)
            .arg("argument with spaces")
            .bounded_output(),
    );
    let measured = success(
        command(root, &plan)
            .args(["Cargo.toml", "--", "argument with spaces"])
            .bounded_output(),
    );
    assert_eq!(plain.stdout, measured.stdout);
    // This combined build/run command retains Cargo diagnostics on stderr.
    // Check the application's distinct stderr record after those diagnostics.
    assert_eq!(plain.stderr, b"application stderr\n");
    assert!(
        measured.stderr.ends_with(&plain.stderr),
        "{}",
        String::from_utf8_lossy(&measured.stderr)
    );
    for (name, text) in files {
        assert_eq!(fs::read_to_string(root.join(name)).unwrap(), text);
    }
    assert_eq!(fs::read(root.join("Cargo.lock")).unwrap(), lock);
    let report: Value =
        serde_json::from_slice(&fs::read(root.join("report.json")).unwrap()).unwrap();
    assert_eq!(report["function_calls"], 2);
    assert_eq!(receiver.spans().len(), 2);
}

#[test]
fn cargo_outside_project_root_is_refused_before_build_or_execution() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let project = root.join("project");
    let launch = root.join("launch");
    fs::create_dir_all(project.join("src")).unwrap();
    fs::create_dir(&launch).unwrap();
    let files = [
        (
            "Cargo.toml",
            "[package]\nname='outside-root'\nversion='0.1.0'\nedition='2024'\n",
        ),
        (
            "Cargo.lock",
            "version=4\n[[package]]\nname='outside-root'\nversion='0.1.0'\n",
        ),
        (
            "src/main.rs",
            "fn main(){std::fs::write(\"application-ran\",\"yes\").unwrap();}\n",
        ),
    ];
    for (name, text) in files {
        fs::write(project.join(name), text).unwrap();
    }
    let receiver = Receiver::new();
    let plan = policy(&project, &receiver);
    let output = command(&launch, &plan)
        .arg(project.join("Cargo.toml"))
        .bounded_output();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("outside the project root"));
    assert!(output.stdout.is_empty());
    assert!(!launch.join("application-ran").exists());
    assert!(!launch.join("report.json").exists());
    assert!(!project.join("target").exists());
    assert!(receiver.spans().is_empty());
    for (name, text) in files {
        assert_eq!(fs::read_to_string(project.join(name)).unwrap(), text);
    }
}
