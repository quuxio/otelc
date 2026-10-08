use super::*;

#[test]
fn cpp_spans_preserve_unedited_exceptions_constructors_cleanup_and_annotations() {
    for (case, optimization, metrics_enabled) in [
        ("exceptions", "-O0", true),
        ("exceptions", "-O2", false),
        ("constructors", "-O0", true),
        ("constructors", "-O2", true),
        ("annotations", "-O2", true),
    ] {
        let (source, includes, mut sizes, errors) = match case {
            "exceptions" => (
                include_str!("../../../../tests/fixtures/exceptions.cpp"),
                "['selected_*','Cleanup::~Cleanup()']",
                vec![3, 3, 4, 4, 4, 4, 12, 12, 12, 12],
                14,
            ),
            "constructors" => (
                include_str!("../../../../examples/apps/cpp-traces.cpp"),
                "['build_order*','choose_value*','Order::*','Base::*']",
                vec![6, 8, 8, 9],
                3,
            ),
            _ => (
                include_str!("../../../../examples/apps/annotated.cpp"),
                "['configured_order*']",
                vec![1; 9],
                1,
            ),
        };
        // Expected cardinality follows the qualified Clang IR: complete and
        // base ABI bodies are distinct invocations, including delegating calls.
        sizes.sort();
        let calls: usize = sizes.iter().sum();
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("main.cpp"), source).unwrap();
        let (metric_port, metric_listener) = receiver();
        let (trace_port, trace_listener) = receiver();
        std::fs::write(root.path().join("otelc.toml"),format!(
            "schema_version=2\nlanguages=['cpp']\n[sources]\ninclude=['*.cpp']\n[functions]\ninclude={includes}\nexclude=['blocked_order*','main']\n[annotations]\nread_existing={}\n[runtime]\nshutdown_timeout_ms=5000\n[metrics]\nenabled={metrics_enabled}\n[traces]\nenabled=true\nroot_sample_ratio=1.0\nmax_active_traces=32\nmax_spans_per_trace=64\n[export]\nendpoint='http://127.0.0.1:{metric_port}'\ninterval_ms=60000\ntimeout_ms=1000\nmax_queued_batches=64\n",
            case=="annotations"
        )).unwrap();
        success(
            Command::new(crate_toolchain().join("clang++"))
                .args([optimization, "-g", "-std=c++17", "main.cpp", "-o", "plain"])
                .current_dir(root.path())
                .output()
                .unwrap(),
        );
        success(cli(
            &[
                "--language",
                "cpp",
                "clang++",
                optimization,
                "-g",
                "-std=c++17",
                "main.cpp",
                "-o",
                "app",
            ],
            root.path(),
        ));
        let inspected = success(cli(&["--language", "cpp", "inspect", "./app"], root.path()));
        let inspected = String::from_utf8(inspected.stdout).unwrap();
        if case == "exceptions" {
            assert!(inspected.contains("Cleanup::~Cleanup() [linkage=_ZN7CleanupD1Ev]"));
            assert!(inspected.contains("Cleanup::~Cleanup() [linkage=_ZN7CleanupD2Ev]"));
            assert!(inspected.contains("configuration name: Cleanup::~Cleanup()"));
            let path = root.path().join("app.otelc.json");
            let original = std::fs::read(&path).unwrap();
            for field in ["selection_name", "display_name"] {
                let mut changed: serde_json::Value = serde_json::from_slice(&original).unwrap();
                let body = changed["functions"]
                    .as_array_mut()
                    .unwrap()
                    .iter_mut()
                    .find(|f| f.get("selection_name").is_some())
                    .unwrap();
                body[field] = serde_json::json!("misidentified function");
                std::fs::write(&path, serde_json::to_vec(&changed).unwrap()).unwrap();
                assert!(
                    !cli(&["--language", "cpp", "inspect", "./app"], root.path())
                        .status
                        .success()
                );
            }
            std::fs::write(&path, original).unwrap();
        }
        retain_native(
            root.path(),
            &format!("cpp-spans-{case}-{optimization}-{metrics_enabled}"),
        );
        let args: &[&str] = if case == "exceptions" {
            &["threads"]
        } else {
            &[]
        };
        let baseline = success(
            Command::new(root.path().join("plain"))
                .args(args)
                .output()
                .unwrap(),
        );
        let metrics = start_receiver(metric_listener);
        let traces = start_trace_receiver(trace_listener, sizes.len(), Duration::from_secs(60));
        let report = root.path().join("report.json");
        let measured = success(
            Command::new(env!("CARGO_BIN_EXE_quux-otelc"))
                .args(["--language", "cpp", "run", "./app"])
                .args(args)
                .env_remove("OTELC_LANGUAGE")
                .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
                .env_remove("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
                .env(
                    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                    format!("http://127.0.0.1:{trace_port}/custom-traces"),
                )
                .env("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "1000")
                .env("OTELC_REPORT_PATH", &report)
                .current_dir(root.path())
                .output()
                .unwrap(),
        );
        assert_eq!(measured.stdout, baseline.stdout, "{case} {optimization}");
        assert_eq!(measured.stderr, baseline.stderr, "{case} {optimization}");
        assert_eq!(
            std::fs::read_to_string(root.path().join("main.cpp")).unwrap(),
            source
        );
        let metrics = metrics.join().unwrap();

        assert_eq!(
            counter(&metrics, "otelc.function.unwinds", None),
            if metrics_enabled { errors } else { 0 }
        );
        let mut found = Vec::new();
        let mut names = Vec::new();
        let mut unwinds = 0;
        for request in traces.join().unwrap() {
            let spans: Vec<_> = request
                .resource_spans
                .iter()
                .flat_map(|r| &r.scope_spans)
                .flat_map(|s| &s.spans)
                .collect();
            found.push(spans.len());
            names.push(
                spans
                    .iter()
                    .map(|span| span.name.clone())
                    .collect::<Vec<_>>(),
            );
            assert_eq!(
                spans.iter().filter(|s| s.parent_span_id.is_empty()).count(),
                1
            );
            let trace = &spans[0].trace_id;
            let ids: std::collections::HashSet<_> = spans.iter().map(|s| &s.span_id).collect();
            assert_eq!(ids.len(), spans.len());
            for span in &spans {
                assert_eq!(span.trace_id.len(), 16);
                assert_eq!(span.span_id.len(), 8);
                assert_eq!(&span.trace_id, trace);
                assert!(span.trace_id.iter().any(|b| *b != 0));
                assert!(span.span_id.iter().any(|b| *b != 0));
                assert!(span.end_time_unix_nano >= span.start_time_unix_nano);
                assert_eq!(span.attributes.len(), 1);
                assert_eq!(span.attributes[0].key, "code.function.name");
                let error = span.status.as_ref().is_some_and(|status| status.code == 2);
                unwinds += u64::from(error);
                if span.name.starts_with("selected_catch")
                    || span.name.starts_with("build_order")
                    || span.name.contains("::~")
                {
                    assert!(
                        !error,
                        "normal catch or cleanup was marked as escaping: {}",
                        span.name
                    );
                }
                if case == "annotations" {
                    assert!(["process_order(", "configured_order(", "throw_order("]
                        .iter()
                        .any(|name| span.name.starts_with(name)));
                }
                if !span.parent_span_id.is_empty() {
                    let parent = spans
                        .iter()
                        .find(|p| p.span_id == span.parent_span_id)
                        .unwrap();
                    assert!(span.start_time_unix_nano >= parent.start_time_unix_nano);
                    assert!(span.end_time_unix_nano <= parent.end_time_unix_nano);
                }
            }
        }
        found.sort();
        assert_eq!(found, sizes, "{case} {optimization}: {names:?}");
        assert_eq!(
            counter(&metrics, "otelc.function.calls", None),
            if metrics_enabled { calls as u64 } else { 0 }
        );
        assert_eq!(unwinds, errors, "{case} {optimization}");
        let report: serde_json::Value =
            serde_json::from_slice(&std::fs::read(report).unwrap()).unwrap();
        assert_eq!(report["export_finished"], true);
        assert_eq!(report["export_dropped_batches"], 0);
        assert_eq!(
            report["function_calls"],
            if metrics_enabled { calls } else { 0 }
        );
        assert_eq!(report["traces"]["completed_trees"], sizes.len());
        assert_eq!(report["traces"]["losses"], serde_json::json!({}));
        assert!(report["losses"]
            .as_object()
            .unwrap()
            .values()
            .all(|n| n == &serde_json::json!(0)));
    }
}

#[test]
fn cpp_unwind_capacity_loss_discards_trees_and_allows_later_healthy_roots() {
    for (kind, stack, spans, mut expected, calls, unwinds, dropped) in [
        ("producer_loss", 2, 64, vec![1, 2, 2], 9, 5, 2),
        ("span_capacity", 32, 1, vec![1], 13, 7, 4),
    ] {
        let root = tempfile::tempdir().unwrap();
        let source = include_str!("../../../../tests/fixtures/exceptions.cpp");
        std::fs::write(root.path().join("main.cpp"), source).unwrap();
        let (port, listener) = receiver();
        let (trace_port, trace_listener) = receiver();
        std::fs::write(root.path().join("otelc.toml"),format!(
            "schema_version=2\nlanguages=['cpp']\n[sources]\ninclude=['*.cpp']\n[functions]\ninclude=['selected_*']\n[runtime]\nshutdown_timeout_ms=5000\n[adapters.cpp.native]\nstack_depth={stack}\n[traces]\nenabled=true\nmax_active_traces=1\nmax_spans_per_trace={spans}\nroot_sample_ratio=1.0\n[export]\nendpoint='http://127.0.0.1:{port}'\ninterval_ms=60000\ntimeout_ms=1000\n"
        )).unwrap();
        success(cli(
            &[
                "--language",
                "cpp",
                "clang++",
                "-O2",
                "-std=c++17",
                "main.cpp",
                "-o",
                "app",
            ],
            root.path(),
        ));
        retain_native(root.path(), &format!("cpp-unwind-{kind}"));
        let metrics = start_receiver(listener);
        let traces = start_trace_receiver(trace_listener, expected.len(), Duration::from_secs(60));
        let report = root.path().join("report.json");
        let output = success(
            Command::new(env!("CARGO_BIN_EXE_quux-otelc"))
                .args(["--language", "cpp", "run", "./app"])
                .env_remove("OTELC_LANGUAGE")
                .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
                .env_remove("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
                .env(
                    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                    format!("http://127.0.0.1:{trace_port}/custom-traces"),
                )
                .env("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "1000")
                .env("OTELC_REPORT_PATH", &report)
                .current_dir(root.path())
                .output()
                .unwrap(),
        );
        assert_eq!(
            String::from_utf8(output.stdout).unwrap().trim(),
            "result=0 cleanup=11"
        );
        assert_eq!(
            std::fs::read_to_string(root.path().join("main.cpp")).unwrap(),
            source
        );
        let metrics = metrics.join().unwrap();
        assert_eq!(counter(&metrics, "otelc.function.calls", None), calls);
        assert_eq!(counter(&metrics, "otelc.function.unwinds", None), unwinds);
        let mut found = Vec::new();
        let mut errors = 0;
        for request in traces.join().unwrap() {
            let tree: Vec<_> = request
                .resource_spans
                .iter()
                .flat_map(|r| &r.scope_spans)
                .flat_map(|s| &s.spans)
                .collect();
            found.push(tree.len());
            assert_eq!(
                tree.iter().filter(|s| s.parent_span_id.is_empty()).count(),
                1
            );
            for span in &tree {
                if !span.parent_span_id.is_empty() {
                    assert!(tree
                        .iter()
                        .any(|parent| parent.span_id == span.parent_span_id));
                }
                errors += usize::from(span.status.as_ref().is_some_and(|status| status.code == 2));
            }
            if tree.len() == 1 {
                assert!(tree[0].name.starts_with("selected_throw"));
                assert!(!tree[0]
                    .status
                    .as_ref()
                    .is_some_and(|status| status.code == 2));
            }
        }
        found.sort();
        expected.sort();
        assert_eq!(found, expected);
        assert_eq!(errors, if kind == "producer_loss" { 3 } else { 0 });
        let report: serde_json::Value =
            serde_json::from_slice(&std::fs::read(report).unwrap()).unwrap();
        assert_eq!(report["traces"]["losses"][kind], dropped);
        assert_eq!(report["traces"]["losses"].as_object().unwrap().len(), 1);
        assert_eq!(report["traces"]["active_trees"], 0);
        assert_eq!(report["export_finished"], true);
    }
}
