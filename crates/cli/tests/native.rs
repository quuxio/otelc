//! Test the actual compiler/runtime boundary with an upstream OTLP decoder.
use opentelemetry_proto::tonic::{
    collector::metrics::v1::ExportMetricsServiceRequest,
    metrics::v1::{metric, number_data_point},
};
use prost::Message;
use std::{
    io::{Read, Write},
    net::TcpListener,
    path::Path,
    process::{Command, Output},
    thread,
    time::Duration,
};
fn cli(args: &[&str], directory: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_quux-otelc"))
        .args(args)
        .current_dir(directory)
        .env_remove("OTELC_LANGUAGE")
        .env_remove("OTELC_CONFIG")
        .env_remove("OTELC_MANIFEST")
        .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
        .env_remove("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
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
fn setup(settings: &str) -> tempfile::TempDir {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(
        root.path().join("main.c"),
        include_str!("../../../tests/fixtures/timing.c"),
    )
    .unwrap();
    std::fs::write(
        root.path().join("main.cpp"),
        include_str!("../../../tests/fixtures/timing.cpp"),
    )
    .unwrap();
    std::fs::write(root.path().join("otelc.toml"),format!("schema_version=1\n[build]\ninclude=[\"*.c\",\"*.cpp\"]\n[functions]\ninclude=[\"selected_*\",\"OrderBook::*\"]\n{settings}\n")).unwrap();
    root
}
fn compile(root: &Path, cpp: bool) {
    if cpp {
        success(cli(
            &[
                "clang++",
                "-O2",
                "-g",
                "-fno-exceptions",
                "main.cpp",
                "-o",
                "app",
            ],
            root,
        ));
    } else {
        success(cli(&["clang", "-O2", "-g", "main.c", "-o", "app"], root));
    }
    if let Some(directory) = std::env::var_os("OTELC_COVERAGE_BIN_DIR") {
        let directory = std::path::PathBuf::from(directory);
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::copy(root.join("app"), directory.join(root.file_name().unwrap())).unwrap();
    }
}
fn receiver() -> (u16, TcpListener) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    (port, listener)
}
fn start_receiver(listener: TcpListener) -> thread::JoinHandle<ExportMetricsServiceRequest> {
    // Start the telemetry deadline after compilation, which can be slow in CI.
    listener.set_nonblocking(true).unwrap();
    thread::spawn(move || {
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        let mut stream = loop {
            match listener.accept() {
                Ok((s, _)) => break s,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "no OTLP export received"
                    );
                    thread::sleep(Duration::from_millis(1));
                }
                Err(e) => panic!("{e}"),
            }
        };
        stream.set_nonblocking(false).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut bytes = Vec::new();
        let mut byte = [0];
        while !bytes.ends_with(b"\r\n\r\n") {
            stream.read_exact(&mut byte).unwrap();
            bytes.push(byte[0]);
            assert!(bytes.len() < 65536);
        }
        let header = String::from_utf8(bytes).unwrap();
        assert!(header.starts_with("POST /v1/metrics HTTP/1.1"));
        let length = header
            .lines()
            .find_map(|l| {
                l.to_lowercase()
                    .strip_prefix("content-length:")
                    .map(|v| v.trim().parse::<usize>().unwrap())
            })
            .unwrap();
        let mut body = vec![0; length];
        stream.read_exact(&mut body).unwrap();
        let request = ExportMetricsServiceRequest::decode(body.as_slice()).unwrap();
        stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-protobuf\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
        request
    })
}
fn run_metrics(settings: &str, cpp: bool, args: &[&str]) -> ExportMetricsServiceRequest {
    let (port, server) = receiver();
    let root = setup(&format!(
        "{settings}\n[export]\nendpoint=\"http://127.0.0.1:{port}\"\ninterval_ms=60000\n"
    ));
    compile(root.path(), cpp);
    let server = start_receiver(server);
    let mut command = vec!["run", "./app"];
    command.extend_from_slice(args);
    let output = success(cli(&command, root.path()));
    assert!(String::from_utf8_lossy(&output.stdout).contains("result="));
    server.join().unwrap()
}
fn counter(request: &ExportMetricsServiceRequest, name: &str, label: Option<&str>) -> u64 {
    let metrics = &request.resource_metrics[0].scope_metrics[0].metrics;
    let Some(metric) = metrics.iter().find(|m| m.name == name) else {
        return 0;
    };
    let Some(metric::Data::Sum(sum)) = &metric.data else {
        panic!("not a sum")
    };
    sum.data_points
        .iter()
        .filter(|p| {
            label.is_none_or(|label| {
                p.attributes
                    .iter()
                    .any(|a| format!("{:?}", a.value).contains(label))
            })
        })
        .map(|p| match p.value {
            Some(number_data_point::Value::AsInt(v)) => v as u64,
            _ => panic!("not an integer"),
        })
        .sum()
}
#[test]
fn c_recursion_counts_and_histograms() {
    for _ in 0..3 {
        let request = run_metrics("", false, &[]);
        assert_eq!(
            counter(&request, "otelc.function.calls", Some("selected_recursive")),
            4
        );
        assert_eq!(
            counter(&request, "otelc.runtime.dropped_observations", None),
            0
        );
        let metrics = &request.resource_metrics[0].scope_metrics[0].metrics;
        let metric = metrics
            .iter()
            .find(|m| m.name == "otelc.function.duration")
            .unwrap();
        let Some(metric::Data::Histogram(histogram)) = &metric.data else {
            panic!("not a histogram")
        };
        assert_eq!(histogram.data_points[0].count, 4);
        assert_eq!(
            histogram.data_points[0].bucket_counts.iter().sum::<u64>(),
            4
        );
        assert!(histogram.data_points[0].sum.unwrap() > 0.0);
    }
}
#[test]
fn cpp_demangled_selection() {
    let request = run_metrics("", true, &[]);
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("OrderBook::add")),
        3
    );
}
#[test]
fn concurrent_threads() {
    let request = run_metrics("[runtime]\nmax_threads=8", false, &["threads"]);
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("selected_work")),
        40
    );
    assert_eq!(
        counter(&request, "otelc.runtime.dropped_observations", None),
        0
    );
}
#[test]
fn stack_limit() {
    let request = run_metrics("[runtime]\nstack_depth=2", false, &[]);
    assert_eq!(counter(&request, "otelc.function.calls", None), 2);
    assert_eq!(
        counter(
            &request,
            "otelc.runtime.dropped_observations",
            Some("stack")
        ),
        2
    );
}
#[test]
fn admission_limit() {
    let request = run_metrics("[runtime]\nmax_threads=1", false, &["threads"]);
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("selected_work")),
        0
    );
    assert_eq!(
        counter(
            &request,
            "otelc.runtime.dropped_observations",
            Some("thread_admission")
        ),
        40
    );
}
#[test]
fn overload_accounting() {
    let request = run_metrics("[runtime]\nqueue_capacity=64", false, &["overload"]);
    let calls = counter(&request, "otelc.function.calls", None);
    let lost = counter(
        &request,
        "otelc.runtime.dropped_observations",
        Some("queue"),
    );
    assert!(lost > 0);
    assert_eq!(calls + lost, 100004);
}
#[test]
fn manifest_stripping_mismatch_and_exit_status() {
    let root = setup("");
    compile(root.path(), false);
    let inspected = success(cli(&["inspect", "app", "--json"], root.path()));
    let value: serde_json::Value = serde_json::from_slice(&inspected.stdout).unwrap();
    assert!(value["functions"]
        .as_array()
        .unwrap()
        .iter()
        .any(|f| f["display_name"] == "excluded_work" && f["selected"] == false));
    assert!(Command::new("strip")
        .arg(root.path().join("app"))
        .status()
        .unwrap()
        .success());
    success(cli(&["inspect", "app"], root.path()));
    assert_eq!(
        cli(&["run", "./app", "exit7"], root.path()).status.code(),
        Some(7)
    );
    let mut value = value;
    value["image_id"] = serde_json::Value::String("incorrect".into());
    std::fs::write(
        root.path().join("app.otelc.json"),
        serde_json::to_vec(&value).unwrap(),
    )
    .unwrap();
    assert!(!cli(&["run", "./app"], root.path()).status.success());
}
#[test]
fn reject_unsupported_and_preserve_compiler_failure() {
    let root = setup("");
    for args in [
        vec!["clang++", "main.cpp", "-o", "app"],
        vec!["clang", "-flto", "main.c", "-o", "app"],
        vec!["clang", "-shared", "main.c", "-o", "app"],
    ] {
        assert!(!cli(&args, root.path()).status.success());
    }
    std::fs::write(root.path().join("bad.c"), "this is not C").unwrap();
    let original = Command::new("clang")
        .args(["-c", "bad.c"])
        .current_dir(root.path())
        .output()
        .unwrap();
    assert_eq!(
        cli(&["clang", "-c", "bad.c"], root.path()).status.code(),
        original.status.code()
    );
    success(cli(&["clang", "-E", "main.c"], root.path()));
    assert!(!root.path().join("app.otelc.json").exists());
}
#[test]
fn compile_link_response_file_and_spaces() {
    let root = setup("");
    std::fs::copy(
        root.path().join("main.c"),
        root.path().join("with spaces.c"),
    )
    .unwrap();
    std::fs::write(
        root.path().join("flags.rsp"),
        "-O2 -g -c 'with spaces.c' -o 'with spaces.o'",
    )
    .unwrap();
    success(cli(&["clang", "@flags.rsp"], root.path()));
    success(cli(&["clang", "with spaces.o", "-o", "app"], root.path()));
    success(cli(&["inspect", "app"], root.path()));
}
#[test]
fn collector_outage_is_bounded() {
    let root=setup("[runtime]\nshutdown_timeout_ms=100\n[export]\nendpoint=\"http://127.0.0.1:1\"\ntimeout_ms=50");
    compile(root.path(), false);
    let start = std::time::Instant::now();
    success(cli(&["run", "./app"], root.path()));
    assert!(start.elapsed() < Duration::from_secs(2));
}

#[test]
fn cli_help_doctor_and_bad_commands() {
    let root = setup("");
    success(cli(&["--help"], root.path()));
    success(cli(&["doctor"], root.path()));
    for args in [
        vec!["unknown"],
        vec!["--config"],
        vec!["doctor", "extra"],
        vec!["inspect"],
        vec!["inspect", "app", "--bad"],
        vec!["run"],
    ] {
        assert!(!cli(&args, root.path()).status.success());
    }
}
#[test]
fn uninstrumented_sources_and_object_integrity() {
    let root = setup("");
    std::fs::write(root.path().join("other.c"), "int other(void){return 1;}\n").unwrap();
    std::fs::write(
        root.path().join("otelc.toml"),
        "schema_version=1\n[build]\ninclude=[\"other.c\"]\n[functions]\ninclude=[\"selected_*\"]\n",
    )
    .unwrap();
    compile(root.path(), false);
    let output = success(cli(&["inspect", "app", "--json"], root.path()));
    let manifest: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert!(manifest["functions"]
        .as_array()
        .unwrap()
        .iter()
        .all(|f| f["selected"] == false));
    assert!(
        !cli(&["clang", "main.c", "other.c", "-o", "app"], root.path())
            .status
            .success()
    );
    success(cli(
        &["clang", "-c", "other.c", "-o", "other.o"],
        root.path(),
    ));
    let marker = root.path().join("other.o.otelc-object.json");
    let mut value: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&marker).unwrap()).unwrap();
    value["digest"] = "wrong".into();
    std::fs::write(marker, serde_json::to_vec(&value).unwrap()).unwrap();
    assert!(
        !cli(&["clang", "main.c", "other.o", "-o", "app"], root.path())
            .status
            .success()
    );
}
#[test]
fn direct_startup_failure_preserves_application() {
    let root = setup("");
    compile(root.path(), false);
    let mut manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(root.path().join("app.otelc.json")).unwrap())
            .unwrap();
    manifest["image_id"] = "wrong".into();
    std::fs::write(
        root.path().join("app.otelc.json"),
        serde_json::to_vec(&manifest).unwrap(),
    )
    .unwrap();
    let output = Command::new(root.path().join("app"))
        .env("OTELC_CONFIG", root.path().join("otelc.toml"))
        .output()
        .unwrap();
    assert!(output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("telemetry disabled"));
}

fn llvm_case(
    source: &str,
    name: &str,
    optimization: &str,
    settings: &str,
    args: &[&str],
) -> ExportMetricsServiceRequest {
    let (port, server) = receiver();
    let root = setup("");
    std::fs::write(root.path().join("case.cpp"), source).unwrap();
    std::fs::write(root.path().join("otelc.toml"), format!("schema_version=1\n[build]\nbackend=\"llvm\"\ninclude=[\"*.cpp\"]\n[functions]\ninclude=[\"selected_*\"]\n{settings}\n[export]\nendpoint=\"http://127.0.0.1:{port}\"\ninterval_ms=60000\n")).unwrap();
    let include = format!("-I{}/../../include", env!("CARGO_MANIFEST_DIR"));
    success(cli(
        &[
            "clang++",
            optimization,
            "-g",
            "-std=c++20",
            &include,
            "case.cpp",
            "-c",
            "-o",
            "case.o",
        ],
        root.path(),
    ));
    success(cli(&["clang++", "case.o", "-o", "app"], root.path()));
    if let Some(directory) = std::env::var_os("OTELC_COVERAGE_BIN_DIR") {
        let directory = std::path::PathBuf::from(directory);
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::copy(
            root.path().join("app"),
            directory.join(format!(
                "{name}-{}",
                root.path().file_name().unwrap().to_string_lossy()
            )),
        )
        .unwrap();
    }
    let server = start_receiver(server);
    let mut run = vec!["run", "./app"];
    run.extend_from_slice(args);
    success(
        Command::new(env!("CARGO_BIN_EXE_quux-otelc"))
            .args(&run)
            .current_dir(root.path())
            .env("OTELC_REPORT_PATH", root.path().join("observations.json"))
            .env_remove("OTELC_CONFIG")
            .env_remove("OTELC_MANIFEST")
            .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
            .env_remove("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
            .output()
            .unwrap(),
    );
    let request = server.join().unwrap();
    let report: serde_json::Value =
        serde_json::from_slice(&std::fs::read(root.path().join("observations.json")).unwrap())
            .unwrap();
    assert_eq!(report["drained"], true);
    assert_eq!(report["export_finished"], true);
    assert_eq!(
        report["function_calls"],
        counter(&request, "otelc.function.calls", None)
    );
    assert_eq!(
        report["object_lifetimes"],
        counter(&request, "otelc.object.lifetimes", None)
    );
    request
}
#[test]
fn llvm_exceptions_recursion_catch_rethrow_and_destructors() {
    for optimization in ["-O0", "-O2"] {
        let request = llvm_case(
            include_str!("../../../tests/fixtures/exceptions.cpp"),
            "exceptions",
            optimization,
            "",
            &[],
        );
        assert_eq!(
            counter(&request, "otelc.function.calls", Some("selected_throw")),
            11
        );
        assert_eq!(
            counter(&request, "otelc.function.unwinds", Some("selected_throw")),
            6
        );
        assert_eq!(
            counter(&request, "otelc.function.calls", Some("selected_catch")),
            1
        );
        assert_eq!(
            counter(&request, "otelc.function.unwinds", Some("selected_catch")),
            0
        );
        assert_eq!(
            counter(&request, "otelc.function.unwinds", Some("selected_rethrow")),
            1
        );
        assert_eq!(
            counter(&request, "otelc.runtime.dropped_observations", None),
            0
        );
    }
}
#[test]
fn llvm_threaded_unwind_and_stack_capacity() {
    let request = llvm_case(
        include_str!("../../../tests/fixtures/exceptions.cpp"),
        "threads",
        "-O2",
        "",
        &["threads"],
    );
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("selected_throw")),
        22
    );
    assert_eq!(
        counter(&request, "otelc.function.unwinds", Some("selected_throw")),
        12
    );
    assert_eq!(
        counter(&request, "otelc.runtime.dropped_observations", None),
        0
    );
    let request = llvm_case(
        include_str!("../../../tests/fixtures/exceptions.cpp"),
        "stack",
        "-O2",
        "[runtime]\nstack_depth=2",
        &[],
    );
    assert_eq!(
        counter(
            &request,
            "otelc.runtime.dropped_observations",
            Some("stack")
        ),
        4
    );
    assert_eq!(
        counter(
            &request,
            "otelc.runtime.dropped_observations",
            Some("invalid_exit")
        ),
        0
    );
    assert_eq!(
        counter(
            &request,
            "otelc.runtime.dropped_observations",
            Some("incomplete")
        ),
        0
    );
}
#[test]
fn object_lifetimes_moves_failed_constructor_unwind_and_threads() {
    let request = llvm_case(
        include_str!("../../../tests/fixtures/objects.cpp"),
        "objects",
        "-O2",
        "[objects]\nclasses=[\"OrderBook\"]\nmax_live=8",
        &[],
    );
    assert_eq!(
        counter(&request, "otelc.object.lifetimes", Some("OrderBook")),
        6
    );
    assert_eq!(
        counter(&request, "otelc.runtime.dropped_observations", None),
        0
    );
}
#[test]
fn object_pool_exhaustion_is_counted_and_reusable() {
    let request = llvm_case(
        include_str!("../../../tests/fixtures/objects.cpp"),
        "object-capacity",
        "-O2",
        "[objects]\nclasses=[\"OrderBook\"]\nmax_live=1",
        &[],
    );
    assert_eq!(
        counter(&request, "otelc.object.lifetimes", Some("OrderBook")),
        5
    );
    assert_eq!(
        counter(
            &request,
            "otelc.runtime.dropped_observations",
            Some("object_capacity")
        ),
        1
    );
}

#[test]
fn llvm_static_function_inventory() {
    let request = llvm_case("static __attribute__((noinline)) int selected_local(int n) { return n + 1; }\nint main() { return selected_local(2) == 3 ? 0 : 1; }", "static", "-O0", "", &[]);
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("selected_local")),
        1
    );
    assert_eq!(
        counter(&request, "otelc.runtime.dropped_observations", None),
        0
    );
}

fn common_config(root: &Path, port: u16) {
    let text = include_str!("../../../examples/common.toml")
        .replace("\"src/**\", \"tests/fixtures/**\"", "\"*.c\", \"*.cpp\"")
        .replace("http://127.0.0.1:4318", &format!("http://127.0.0.1:{port}"))
        .replace("interval_ms = 1000", "interval_ms = 60000");
    std::fs::write(root.join("otelc.toml"), text).unwrap();
}
#[test]
fn common_configuration_inspection_and_capability_rejection() {
    let root = setup("");
    common_config(root.path(), 4318);
    for language in [
        "c",
        "cpp",
        "rust",
        "typescript",
        "javascript",
        "java",
        "python",
        "go",
    ] {
        let output = success(cli(
            &["--language", language, "config", "--json"],
            root.path(),
        ));
        let policy: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(policy["language"], language);
        assert_eq!(policy["schema_version"], 2);
        assert_eq!(
            policy["metrics_endpoint"],
            "http://127.0.0.1:4318/v1/metrics"
        );
        assert_eq!(policy["resource"]["service_name"], "otelc-common-example");
        assert_eq!(
            policy["execution_available"],
            language == "c"
                || language == "cpp"
                || language == "python"
                || language == "javascript"
                || language == "typescript"
                || language == "java"
                || language == "go"
        );
    }
    success(cli(
        &["--language", "cpp", "config", "--require-supported"],
        root.path(),
    ));
    let unavailable = cli(
        &["--language", "rust", "config", "--require-supported"],
        root.path(),
    );
    assert!(!unavailable.status.success());
    assert!(
        String::from_utf8_lossy(&unavailable.stdout).contains("rust adapter is not implemented")
    );
    for args in [
        vec!["config"],
        vec!["--language", "scala", "config"],
        vec!["--language", "cpp", "config", "--unknown"],
        vec!["--config"],
        vec!["--language", "cpp", "run", "./absent"],
    ] {
        assert!(!cli(&args, root.path()).status.success());
    }
    let path = root.path().join("otelc.toml");
    let text = std::fs::read_to_string(&path).unwrap();
    std::fs::write(
        &path,
        text.replace("inject_generated = false", "inject_generated = true"),
    )
    .unwrap();
    let output = cli(&["--language", "cpp", "doctor"], root.path());
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("annotation injection is not implemented")
    );
}
#[test]
fn common_configuration_native_c_and_exception_enabled_cpp() {
    for language in ["c", "cpp"] {
        let (port, server) = receiver();
        let root = setup("");
        common_config(root.path(), port);
        let cpp = language == "cpp";
        if cpp {
            std::fs::write(
                root.path().join("main.cpp"),
                include_str!("../../../tests/fixtures/exceptions.cpp"),
            )
            .unwrap();
        }
        let source = if cpp { "main.cpp" } else { "main.c" };
        let before = std::fs::read(root.path().join(source)).unwrap();
        let mut args = vec![if cpp { "clang++" } else { "clang" }, "-O2", "-g"];
        if cpp {
            args.push("-std=c++20");
        }
        args.extend([source, "-c", "-o", "case.o"]);
        success(cli(&args, root.path()));
        success(cli(
            &[if cpp { "clang++" } else { "clang" }, "case.o", "-o", "app"],
            root.path(),
        ));
        if let Some(directory) = std::env::var_os("OTELC_COVERAGE_BIN_DIR") {
            let directory = std::path::PathBuf::from(directory);
            std::fs::create_dir_all(&directory).unwrap();
            std::fs::copy(
                root.path().join("app"),
                directory.join(format!(
                    "common-{language}-{}",
                    root.path().file_name().unwrap().to_string_lossy()
                )),
            )
            .unwrap();
        }
        assert_eq!(before, std::fs::read(root.path().join(source)).unwrap());
        let server = start_receiver(server);
        success(cli(&["--language", language, "run", "./app"], root.path()));
        let request = server.join().unwrap();
        assert_eq!(
            counter(&request, "otelc.runtime.dropped_observations", None),
            0
        );
        if cpp {
            assert_eq!(
                counter(&request, "otelc.function.calls", Some("selected_throw")),
                11
            );
            assert_eq!(
                counter(&request, "otelc.function.unwinds", Some("selected_throw")),
                6
            );
        } else {
            assert_eq!(
                counter(&request, "otelc.function.calls", Some("selected_recursive")),
                4
            );
        }
        // Conflicting adapter/driver choices must not compile using another policy.
        assert!(!cli(
            &[
                "--language",
                "python",
                "clang",
                "main.c",
                "-c",
                "-o",
                "bad.o"
            ],
            root.path()
        )
        .status
        .success());
        assert!(!cli(&["run", "./app"], root.path()).status.success());
    }
}

#[test]
fn node_adapters_preserve_source_and_export_decodable_sdk_metrics() {
    for (language, command, extension) in
        [("javascript", "node", "cjs"), ("typescript", "ts", "cts")]
    {
        let root = tempfile::tempdir().unwrap();
        let source = "function selected(n){if(n<0)throw new Error('escaping');return n*2;}\n// otelc.instrument\nfunction annotated(){return 3;}\nconsole.log(selected(3)+annotated());try{selected(-1);}catch(error){if(error.message!=='escaping')throw error;}\n";
        let source = if language == "typescript" {
            source
                .replace("selected(n)", "selected(n:number)")
                .replace("annotated()", "annotated():number")
                .replace("+annotated():number", "+annotated()")
        } else {
            source.to_owned()
        };
        let filename = format!("app.{extension}");
        std::fs::write(root.path().join(&filename), &source).unwrap();
        let (port, listener) = receiver();
        std::fs::write(root.path().join("otelc.toml"), format!("schema_version=2\nlanguages=['{language}']\n[sources]\ninclude=['*.{extension}']\n[functions]\ninclude=['app.selected']\n[annotations]\nread_existing=true\n[export]\nendpoint='http://127.0.0.1:{port}'\ninterval_ms=60000\ntimeout_ms=500\n")).unwrap();
        let baseline = success(
            Command::new("node")
                .arg(&filename)
                .current_dir(root.path())
                .output()
                .unwrap(),
        );
        let server = start_receiver(listener);
        let instrumented = success(cli(&[command, &filename], root.path()));
        assert_eq!(baseline.stdout, instrumented.stdout);
        assert_eq!(
            source.as_bytes(),
            std::fs::read(root.path().join(&filename)).unwrap()
        );
        let request = server.join().unwrap();
        assert_eq!(
            counter(&request, "otelc.function.calls", Some("app.selected")),
            2
        );
        assert_eq!(
            counter(&request, "otelc.function.calls", Some("app.annotated")),
            1
        );
        assert_eq!(
            counter(&request, "otelc.function.unwinds", Some("app.selected")),
            1
        );
        success(cli(&["--language", language, "doctor"], root.path()));
        let inspected = success(cli(
            &["--language", language, "inspect", &filename, "--json"],
            root.path(),
        ));
        let inventory: serde_json::Value = serde_json::from_slice(&inspected.stdout).unwrap();
        assert_eq!(inventory["functions"].as_array().unwrap().len(), 2);
        for args in [
            vec![command],
            vec![command, "--eval", "1"],
            vec!["--language", "python", command, &filename],
        ] {
            assert!(!cli(&args, root.path()).status.success());
        }
    }
}

#[test]
fn java_agent_preserves_original_sources_and_exports_typed_sdk_metrics() {
    let root = tempfile::tempdir().unwrap();
    let source = "public class App { public static int selected(int n){if(n<0)throw new IllegalArgumentException(\"escaping\");return n*2;} @OtelcInstrument public static int annotated(){return 3;} public static void main(String[] args){System.out.println(selected(3)+annotated());try{selected(-1);}catch(IllegalArgumentException error){if(!error.getMessage().equals(\"escaping\"))throw error;}} } @interface OtelcInstrument {}";
    std::fs::write(root.path().join("App.java"), source).unwrap();
    success(
        Command::new("javac")
            .args(["-g", "App.java"])
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    let (port, listener) = receiver();
    std::fs::write(root.path().join("otelc.toml"), format!("schema_version=2\nlanguages=['java']\n[sources]\ninclude=['*.java']\n[functions]\ninclude=['App.selected(*)']\n[annotations]\nread_existing=true\n[export]\nendpoint='http://127.0.0.1:{port}'\ninterval_ms=60000\ntimeout_ms=500\n")).unwrap();
    let baseline = success(
        Command::new("java")
            .arg("App")
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    let server = start_receiver(listener);
    let measured = success(cli(&["java", "App"], root.path()));
    assert_eq!(baseline.stdout, measured.stdout);
    assert_eq!(
        source.as_bytes(),
        std::fs::read(root.path().join("App.java")).unwrap()
    );
    let request = server.join().unwrap();
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("App.selected(int)")),
        2
    );
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("App.annotated()")),
        1
    );
    assert_eq!(
        counter(
            &request,
            "otelc.function.unwinds",
            Some("App.selected(int)")
        ),
        1
    );
    success(cli(&["--language", "java", "doctor"], root.path()));
    let inventory = success(cli(
        &["--language", "java", "inspect", "App.class", "--json"],
        root.path(),
    ));
    let parsed: serde_json::Value = serde_json::from_slice(&inventory.stdout).unwrap();
    assert_eq!(parsed["functions"].as_array().unwrap().len(), 4);
    for args in [vec!["java"], vec!["--language", "python", "java", "App"]] {
        assert!(!cli(&args, root.path()).status.success());
    }
}

#[test]
fn go_overlay_preserves_sources_recovery_and_decodable_sdk_counters() {
    let root = tempfile::tempdir().unwrap();
    let source = "package main\nimport \"fmt\"\nfunc selected(n int)int{if n<0{panic(\"escaping\")};return n*2}\n// otelc.instrument\nfunc annotated()int{return 3}\nfunc main(){fmt.Println(selected(3)+annotated());func(){defer func(){if recover()!=\"escaping\"{panic(\"changed\")}}();selected(-1)}()}\n";
    std::fs::write(root.path().join("app.go"), source).unwrap();
    let (port, listener) = receiver();
    std::fs::write(root.path().join("otelc.toml"), format!("schema_version=2\nlanguages=['go']\n[sources]\ninclude=['*.go']\n[functions]\ninclude=['app.selected']\n[annotations]\nread_existing=true\n[export]\nendpoint='http://127.0.0.1:{port}'\ninterval_ms=60000\ntimeout_ms=500\n")).unwrap();
    let baseline = success(
        Command::new("go")
            .args(["run", "app.go"])
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    let server = start_receiver(listener);
    let measured = success(cli(&["go", "app.go"], root.path()));
    assert_eq!(baseline.stdout, measured.stdout);
    assert_eq!(
        source.as_bytes(),
        std::fs::read(root.path().join("app.go")).unwrap()
    );
    let request = server.join().unwrap();
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("app.selected")),
        2
    );
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("app.annotated")),
        1
    );
    assert_eq!(
        counter(&request, "otelc.function.unwinds", Some("app.selected")),
        1
    );
    success(cli(&["--language", "go", "doctor"], root.path()));
    let inventory = success(cli(
        &["--language", "go", "inspect", "app.go", "--json"],
        root.path(),
    ));
    let parsed: serde_json::Value = serde_json::from_slice(&inventory.stdout).unwrap();
    assert!(parsed["functions"]
        .as_array()
        .unwrap()
        .iter()
        .any(|f| f["name"] == "app.annotated" && f["selected"] == true));
    for args in [vec!["go"], vec!["--language", "python", "go", "app.go"]] {
        assert!(!cli(&args, root.path()).status.success());
    }
}

#[test]
fn tutorial_config_and_existing_annotations_preserve_source_and_plain_results() {
    for (source, template, cpp) in [
        (
            include_str!("../../../examples/apps/config-only.c"),
            include_str!("../../../examples/config-only.toml"),
            false,
        ),
        (
            include_str!("../../../examples/apps/annotated.c"),
            include_str!("../../../examples/annotated.toml"),
            false,
        ),
        (
            include_str!("../../../examples/apps/annotated.cpp"),
            include_str!("../../../examples/annotated.toml"),
            true,
        ),
    ] {
        for read in [false, true] {
            let (port, server) = receiver();
            let root = setup("");
            let filename = if cpp { "tutorial.cpp" } else { "tutorial.c" };
            std::fs::write(root.path().join(filename), source).unwrap();
            let text = template
                .replace("examples/apps/**", r#"*.c", "*.cpp"#)
                .replace("http://127.0.0.1:4318", &format!("http://127.0.0.1:{port}"))
                .replace("interval_ms = 1000", "interval_ms = 60000")
                .replace("read_existing = true", &format!("read_existing = {read}"));
            std::fs::write(root.path().join("otelc.toml"), text).unwrap();
            let toolchain = crate_toolchain();
            let plain = Command::new(toolchain.join(if cpp { "clang++" } else { "clang" }))
                .args(["-O2", "-g", filename, "-o", "plain"])
                .current_dir(root.path())
                .output()
                .unwrap();
            success(plain);
            let expected =
                success(Command::new(root.path().join("plain")).output().unwrap()).stdout;
            success(cli(
                &[
                    if cpp { "clang++" } else { "clang" },
                    "-O2",
                    "-g",
                    filename,
                    "-c",
                    "-o",
                    "case.o",
                ],
                root.path(),
            ));
            success(cli(
                &[if cpp { "clang++" } else { "clang" }, "case.o", "-o", "app"],
                root.path(),
            ));
            retain_native(root.path(), &format!("annotations-{cpp}-{read}"));
            assert_eq!(
                std::fs::read_to_string(root.path().join(filename)).unwrap(),
                source
            );
            let inspection: serde_json::Value = serde_json::from_slice(
                &success(cli(&["inspect", "app", "--json"], root.path())).stdout,
            )
            .unwrap();
            if template.contains("read_existing = true") && read {
                assert!(inspection["functions"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|f| f["selected"] == true
                        && f["annotated"] == true
                        && f["reason"] == "included by annotation"));
            }
            let language = if cpp { "cpp" } else { "c" };
            let server = start_receiver(server);
            assert_eq!(
                success(cli(&["--language", language, "run", "./app"], root.path())).stdout,
                expected
            );
            let request = server.join().unwrap();
            assert_eq!(
                counter(&request, "otelc.runtime.dropped_observations", None),
                0
            );
            let annotated = template.contains("read_existing = true");
            assert_eq!(
                counter(&request, "otelc.function.calls", Some("process_order")),
                if !annotated || read { 4 } else { 0 }
            );
            assert_eq!(
                counter(&request, "otelc.function.calls", Some("audit_order")),
                if annotated && !read { 4 } else { 0 }
            );
            assert_eq!(
                counter(&request, "otelc.function.calls", Some("blocked_order")),
                0
            );
            assert_eq!(
                counter(&request, "otelc.function.calls", Some("vendor_order")),
                0
            );
            assert_eq!(
                counter(&request, "otelc.function.calls", Some("conflicted_order")),
                if annotated && !read { 4 } else { 0 }
            );
            if annotated {
                assert_eq!(
                    counter(&request, "otelc.function.calls", Some("configured_order")),
                    4
                );
            }
            if cpp {
                assert_eq!(
                    counter(&request, "otelc.function.unwinds", Some("throw_order")),
                    if read { 1 } else { 0 }
                );
            }
        }
    }
}
fn crate_toolchain() -> std::path::PathBuf {
    let directory = Path::new(env!("CARGO_BIN_EXE_quux-otelc"))
        .parent()
        .unwrap();
    let value: serde_json::Value = serde_json::from_slice(
        &std::fs::read(directory.join("otelc-llvm-toolchain.json")).unwrap(),
    )
    .unwrap();
    value["bindir"].as_str().unwrap().into()
}
fn retain_native(root: &Path, name: &str) {
    if let Some(directory) = std::env::var_os("OTELC_COVERAGE_BIN_DIR") {
        let directory = std::path::PathBuf::from(directory);
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::copy(
            root.join("app"),
            directory.join(format!(
                "{name}-{}",
                root.file_name().unwrap().to_string_lossy()
            )),
        )
        .unwrap();
    }
}
#[test]
fn live_metrics_toggle_preserves_inflight_calls_and_exception_tokens() {
    use std::io::BufRead;
    use std::process::Stdio;
    let (port, server) = receiver();
    let root = setup("");
    let source = include_str!("../../../examples/apps/live-latency.cpp");
    std::fs::write(root.path().join("live.cpp"), source).unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let socket = root.path().join("metrics.sock");
    let config = include_str!("../../../examples/live.toml")
        .replace("examples/apps/live-latency.cpp", "*.cpp")
        .replace("build/control/metrics.sock", socket.to_str().unwrap())
        .replace("http://127.0.0.1:4318", &format!("http://127.0.0.1:{port}"))
        .replace("interval_ms = 1000", "interval_ms = 60000");
    std::fs::write(root.path().join("otelc.toml"), config).unwrap();
    success(cli(
        &["clang++", "-O2", "-std=c++20", "live.cpp", "-o", "app"],
        root.path(),
    ));
    retain_native(root.path(), "live-control");
    let report = root.path().join("report.json");
    let server = start_receiver(server);
    let mut child = Command::new(env!("CARGO_BIN_EXE_quux-otelc"))
        .args(["--language", "cpp", "run", "./app"])
        .env_remove("OTELC_LANGUAGE")
        .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
        .env_remove("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
        .env("OTELC_REPORT_PATH", &report)
        .current_dir(root.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut input = child.stdin.take().unwrap();
    let mut output = std::io::BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    output.read_line(&mut line).unwrap();
    assert_eq!(line.trim(), "ready");
    let control = |command| -> serde_json::Value {
        serde_json::from_slice(
            &success(cli(
                &[command, "--socket", socket.to_str().unwrap()],
                root.path(),
            ))
            .stdout,
        )
        .unwrap()
    };
    assert_eq!(control("status")["pid"], child.id());
    assert_eq!(control("status")["metrics_enabled"], false);
    writeln!(input, "batch 100").unwrap();
    line.clear();
    output.read_line(&mut line).unwrap();
    assert!(line.contains("calls=100"));
    assert_eq!(control("status")["function_calls"], 0);
    assert_eq!(control("enable")["metrics_enabled"], true);
    writeln!(input, "batch 100").unwrap();
    line.clear();
    output.read_line(&mut line).unwrap();
    writeln!(input, "hold").unwrap();
    line.clear();
    output.read_line(&mut line).unwrap();
    assert_eq!(line.trim(), "holding");
    assert_eq!(control("disable")["metrics_enabled"], false);
    writeln!(input, "throw").unwrap();
    line.clear();
    output.read_line(&mut line).unwrap();
    assert_eq!(line.trim(), "caught");
    writeln!(input, "hold").unwrap();
    line.clear();
    output.read_line(&mut line).unwrap();
    assert_eq!(line.trim(), "holding");
    assert_eq!(control("enable")["metrics_enabled"], true);
    writeln!(input, "throw").unwrap();
    line.clear();
    output.read_line(&mut line).unwrap();
    assert_eq!(line.trim(), "caught");
    writeln!(input, "batch 50").unwrap();
    line.clear();
    output.read_line(&mut line).unwrap();
    assert_eq!(control("disable")["metrics_enabled"], false);
    writeln!(input, "batch 50").unwrap();
    line.clear();
    output.read_line(&mut line).unwrap();
    assert_eq!(control("status")["pid"], child.id());
    writeln!(input, "quit").unwrap();
    assert!(child.wait().unwrap().success());
    assert!(!socket.exists());
    assert_eq!(
        std::fs::read_to_string(root.path().join("live.cpp")).unwrap(),
        source
    );
    let report: serde_json::Value =
        serde_json::from_slice(&std::fs::read(report).unwrap()).unwrap();
    assert_eq!(report["function_calls"], 151);
    assert_eq!(report["drained"], true);
    assert!(report["losses"]
        .as_object()
        .unwrap()
        .values()
        .all(|n| n == 0));
    let request = server.join().unwrap();
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("process_order")),
        150
    );
    assert_eq!(
        counter(&request, "otelc.function.calls", Some("held_order")),
        1
    );
    assert_eq!(
        counter(&request, "otelc.function.unwinds", Some("held_order")),
        1
    );
    assert_eq!(
        counter(&request, "otelc.runtime.dropped_observations", None),
        0
    );
    for args in [
        vec!["enable"],
        vec!["disable", "--socket", "absent"],
        vec!["status", "--socket", "otelc.toml"],
    ] {
        assert!(!cli(&args, root.path()).status.success());
    }
}
#[test]
fn invalid_annotation_metadata_and_control_responses_are_rejected() {
    use std::os::unix::{fs::PermissionsExt, net::UnixListener};
    let root = setup("");
    let config = include_str!("../../../examples/annotated.toml")
        .replace("examples/apps/**", r#"*.c", "*.cpp"#);
    std::fs::write(root.path().join("otelc.toml"), config).unwrap();
    for source in [
        "__attribute__((annotate(\"otelc.unknown\"))) int f(void){return 1;}",
        "__attribute__((annotate(\"otelc.instrument\"))) int variable=1;",
    ] {
        std::fs::write(root.path().join("bad.c"), source).unwrap();
        let output = cli(&["clang", "bad.c", "-c", "-o", "bad.o"], root.path());
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("otelc:"));
        assert_eq!(
            std::fs::read_to_string(root.path().join("bad.c")).unwrap(),
            source
        );
    }
    for response in [
        "not-json\n",
        "{\"schema_version\":2,\"metrics_enabled\":true}\n",
        "{\"error\":\"invalid\"}\n",
        "{\"schema_version\":1,\"metrics_enabled\":true}\n",
    ] {
        let path = root.path().join("fake.sock");
        let server = UnixListener::bind(&path).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        let handle = thread::spawn(move || {
            let (mut stream, _) = server.accept().unwrap();
            let mut bytes = [0; 7];
            stream.read_exact(&mut bytes).unwrap();
            stream.write_all(response.as_bytes()).unwrap();
        });
        assert!(
            !cli(&["status", "--socket", path.to_str().unwrap()], root.path())
                .status
                .success()
        );
        handle.join().unwrap();
        std::fs::remove_file(path).unwrap();
    }
}
