use super::*;
use quux_otelc_symbols::{mark_object, object_probes};

fn transform(root: &Path, input: &str, output: &str, name: &str) -> Output {
    cli(
        &[
            "object",
            "rename-main",
            input,
            "--output",
            output,
            "--symbol",
            name,
        ],
        root,
    )
}
fn tool(name: &str) -> std::path::PathBuf {
    let cli = Path::new(env!("CARGO_BIN_EXE_quux-otelc"));
    let metadata: serde_json::Value = serde_json::from_slice(
        &std::fs::read(cli.parent().unwrap().join("otelc-llvm-toolchain.json")).unwrap(),
    )
    .unwrap();
    Path::new(metadata["bindir"].as_str().unwrap()).join(name)
}

#[test]
fn renamed_main_preserves_implicit_return_cpp_unwind_selection_and_actual_spans() {
    for cpp in [false, true] {
        let root = tempfile::tempdir().unwrap();
        let (language, compiler, extension) = if cpp {
            ("cpp", "clang++", "cpp")
        } else {
            ("c", "clang", "c")
        };
        let source = if cpp {
            "#include <stdexcept>\nint value=3; struct Cleanup{~Cleanup(){++value;}};\n__attribute__((noinline)) int selected_work(int n){Cleanup c; if(n<0)throw std::runtime_error(\"original\");value+=n;return value;}\nint state(){return value;}\nint main(){selected_work(5);}\n"
        } else {
            "int value=3;\n__attribute__((noinline)) int selected_work(int n){value+=n;return value;}\nint state(void){return value;}\nint main(void){selected_work(5);}\n"
        };
        let driver = if cpp {
            "#include <cstdio>\n#include <string>\n#include <stdexcept>\nextern \"C\" int __oteleq_original_main();int selected_work(int);int state();\nint main(){int r=__oteleq_original_main();int error=0;try{selected_work(-1);}catch(const std::runtime_error& e){error=std::string(e.what())==\"original\";}std::printf(\"ret=%d state=%d error=%d\\n\",r,state(),error);return r!=0||state()!=10||!error;}\n"
        } else {
            "#include <stdio.h>\nint __oteleq_original_main(void);int state(void);\nint main(void){int r=__oteleq_original_main();printf(\"ret=%d state=%d\\n\",r,state());return r!=0||state()!=8;}\n"
        };
        let source_name = format!("original.{extension}");
        let driver_name = format!("driver.{extension}");
        std::fs::write(root.path().join(&source_name), source).unwrap();
        std::fs::write(root.path().join(&driver_name), driver).unwrap();
        let plain_compile = |arguments: &[&str]| {
            success(
                Command::new(tool(compiler))
                    .args(arguments)
                    .current_dir(root.path())
                    .output()
                    .unwrap(),
            )
        };
        plain_compile(&["-O0", "-g", "-c", &source_name, "-o", "plain-original.o"]);
        success(
            Command::new(tool("llvm-objcopy"))
                .args([
                    "--redefine-sym",
                    if cfg!(target_os = "macos") {
                        "_main=___oteleq_original_main"
                    } else {
                        "main=__oteleq_original_main"
                    },
                    "plain-original.o",
                    "plain.o",
                ])
                .current_dir(root.path())
                .output()
                .unwrap(),
        );
        plain_compile(&["-O0", "-g", "-c", &driver_name, "-o", "driver.o"]);
        plain_compile(&["plain.o", "driver.o", "-o", "baseline"]);
        let baseline = success(Command::new(root.path().join("baseline")).output().unwrap());
        let (metric_port, metric_listener) = receiver();
        let (trace_port, trace_listener) = receiver();
        std::fs::write(root.path().join("otelc.toml"), format!("schema_version=2\nlanguages=['{language}']\n[sources]\ninclude=['*.{extension}']\n[functions]\ninclude=['main','selected_work*']\n[traces]\nenabled=true\nroot_sample_ratio=1.0\n[export]\nendpoint='http://127.0.0.1:{metric_port}'\ninterval_ms=60000\ntimeout_ms=1000\n")).unwrap();
        success(cli(
            &[
                "--language",
                language,
                compiler,
                "-O0",
                "-g",
                "-c",
                &source_name,
                "-o",
                "original.o",
            ],
            root.path(),
        ));
        let original = std::fs::read(root.path().join("original.o")).unwrap();
        let original_marker =
            std::fs::read(root.path().join("original.o.otelc-object.json")).unwrap();
        success(transform(
            root.path(),
            "original.o",
            "renamed.o",
            "__oteleq_original_main",
        ));
        assert_eq!(
            original,
            std::fs::read(root.path().join("original.o")).unwrap()
        );
        assert_eq!(
            original_marker,
            std::fs::read(root.path().join("original.o.otelc-object.json")).unwrap()
        );
        let probes = object_probes(&root.path().join("renamed.o"), "llvm").unwrap();
        assert_eq!(
            probes.main_renames.get("__oteleq_original_main").unwrap(),
            "main"
        );
        success(cli(
            &[
                "--language",
                language,
                compiler,
                "renamed.o",
                "driver.o",
                "-o",
                "app",
            ],
            root.path(),
        ));
        let manifest =
            quux_otelc_symbols::Manifest::load(&root.path().join("app.otelc.json")).unwrap();
        let main = manifest
            .functions
            .iter()
            .find(|f| f.linkage_name == "__oteleq_original_main")
            .unwrap();
        assert!(main.selected);
        assert_eq!(main.selection_name.as_deref(), Some("main"));
        assert_eq!(main.display_name, "main [linkage=__oteleq_original_main]");
        let traces = start_trace_receiver(
            trace_listener,
            if cpp { 2 } else { 1 },
            Duration::from_secs(20),
        );
        let metrics = start_receiver(metric_listener);
        let output = success(
            Command::new(env!("CARGO_BIN_EXE_quux-otelc"))
                .args(["--language", language, "run", "./app"])
                .env(
                    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                    format!("http://127.0.0.1:{trace_port}/custom-traces"),
                )
                .env_remove("OTELC_LANGUAGE")
                .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
                .env_remove("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
                .current_dir(root.path())
                .output()
                .unwrap(),
        );
        assert_eq!(baseline.stdout, output.stdout);
        assert_eq!(baseline.stderr, output.stderr);
        assert_eq!(
            source,
            std::fs::read_to_string(root.path().join(&source_name)).unwrap()
        );
        assert_eq!(
            driver,
            std::fs::read_to_string(root.path().join(&driver_name)).unwrap()
        );
        assert_eq!(
            counter(&metrics.join().unwrap(), "otelc.function.calls", None),
            if cpp { 3 } else { 2 }
        );
        let requests = traces.join().unwrap();
        let spans: Vec<_> = requests
            .iter()
            .flat_map(|r| &r.resource_spans)
            .flat_map(|r| &r.scope_spans)
            .flat_map(|s| &s.spans)
            .collect();
        assert_eq!(spans.len(), if cpp { 3 } else { 2 });
        assert_eq!(
            spans
                .iter()
                .filter(|s| s.name == "main [linkage=__oteleq_original_main]")
                .count(),
            1
        );
        assert_eq!(
            spans
                .iter()
                .filter(|s| s.status.as_ref().is_some_and(|s| s.code == 2))
                .count(),
            usize::from(cpp)
        );
        if let Some(directory) = std::env::var_os("OTELC_COVERAGE_BIN_DIR") {
            let directory = std::path::PathBuf::from(directory);
            std::fs::create_dir_all(&directory).unwrap();
            std::fs::copy(
                root.path().join("app"),
                directory.join(root.path().file_name().unwrap()),
            )
            .unwrap();
        }
    }
}

#[test]
fn object_transform_refuses_missing_stale_collision_chained_and_occupied_outputs() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(
        root.path().join("main.c"),
        "int existing(void){return 1;} int main(void){return 0;}",
    )
    .unwrap();
    success(
        Command::new(tool("clang"))
            .args(["-O0", "-c", "main.c", "-o", "original.o"])
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    assert!(
        !transform(root.path(), "original.o", "new.o", "renamed_main")
            .status
            .success()
    );
    mark_object(&root.path().join("original.o"), false, "llvm").unwrap();
    for name in ["main", "", "bad-name", "existing"] {
        assert!(!transform(root.path(), "original.o", "new.o", name)
            .status
            .success());
    }
    let bytes = std::fs::read(root.path().join("original.o")).unwrap();
    let mut stale = bytes.clone();
    stale.push(0);
    std::fs::write(root.path().join("original.o"), stale).unwrap();
    assert!(
        !transform(root.path(), "original.o", "new.o", "renamed_main")
            .status
            .success()
    );
    std::fs::write(root.path().join("original.o"), bytes).unwrap();
    std::fs::write(root.path().join("new.o"), "keep").unwrap();
    assert!(
        !transform(root.path(), "original.o", "new.o", "renamed_main")
            .status
            .success()
    );
    assert_eq!(
        std::fs::read_to_string(root.path().join("new.o")).unwrap(),
        "keep"
    );
    std::fs::remove_file(root.path().join("new.o")).unwrap();
    success(transform(
        root.path(),
        "original.o",
        "new.o",
        "renamed_main",
    ));
    assert!(!transform(root.path(), "new.o", "again.o", "second_main")
        .status
        .success());
    let marker = root.path().join("new.o.otelc-object.json");
    let mut value: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&marker).unwrap()).unwrap();
    value["transformation"]["source_digest"] = "bad".into();
    std::fs::write(marker, serde_json::to_vec(&value).unwrap()).unwrap();
    assert!(object_probes(&root.path().join("new.o"), "llvm").is_err());
}

#[test]
fn absent_main_and_elf_comdat_objects_keep_exact_structure_and_valid_markers() {
    for (source,target,extra) in [("int work(void){return 4;}","unused_main",vec![]),
        ("template<int N> __attribute__((noinline)) int value(){return N;} int main(){return value<3>();}","renamed_main",vec!["--target=x86_64-unknown-linux-gnu","-ffreestanding"])] {
        let root=tempfile::tempdir().unwrap();std::fs::write(root.path().join("main.cpp"),source).unwrap();
        success(Command::new(tool("clang++")).args(extra).args(["-O0","-g","-c","main.cpp","-o","original.o"]).current_dir(root.path()).output().unwrap());
        mark_object(&root.path().join("original.o"),false,"llvm").unwrap();
        success(transform(root.path(),"original.o","new.o",target));
        assert!(object_probes(&root.path().join("new.o"),"llvm").unwrap().functions.is_empty());
    }
}

#[test]
fn object_transform_keeps_annotation_and_exclusion_policy_under_original_main_name() {
    for excluded in [false, true] {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(
            root.path().join("main.c"),
            "__attribute__((annotate(\"otelc.instrument\"))) int main(void){return 0;}\n",
        )
        .unwrap();
        std::fs::write(
            root.path().join("driver.c"),
            "int renamed_main(void); int main(void){return renamed_main();}\n",
        )
        .unwrap();
        let exclusion = if excluded { "['main']" } else { "[]" };
        std::fs::write(root.path().join("otelc.toml"), format!("schema_version=2\nlanguages=['c']\n[sources]\ninclude=['main.c']\n[functions]\ninclude=[]\nexclude={exclusion}\n[annotations]\nread_existing=true\n")).unwrap();
        success(cli(
            &["clang", "-O0", "-c", "main.c", "-o", "original.o"],
            root.path(),
        ));
        success(transform(
            root.path(),
            "original.o",
            "new.o",
            "renamed_main",
        ));
        success(
            Command::new(tool("clang"))
                .args(["-c", "driver.c", "-o", "driver.o"])
                .current_dir(root.path())
                .output()
                .unwrap(),
        );
        success(cli(
            &["clang", "new.o", "driver.o", "-o", "app"],
            root.path(),
        ));
        let path = root.path().join("app.otelc.json");
        let manifest = quux_otelc_symbols::Manifest::load(&path).unwrap();
        let selected = manifest
            .functions
            .iter()
            .filter(|f| f.selected)
            .collect::<Vec<_>>();
        assert_eq!(selected.len(), usize::from(!excluded));
        if !excluded {
            assert_eq!(selected[0].selection_name.as_deref(), Some("main"));
            assert!(selected[0].annotated);
            assert_eq!(selected[0].reason, "included by annotation");
            let original = std::fs::read(&path).unwrap();
            for invalid in ["bad-name", "other"] {
                let mut value: serde_json::Value = serde_json::from_slice(&original).unwrap();
                value["main_renames"]["renamed_main"] = invalid.into();
                std::fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
                assert!(quux_otelc_symbols::Manifest::load(&path).is_err());
            }
        }
        assert_eq!(
            success(Command::new(root.path().join("app")).output().unwrap())
                .status
                .code(),
            Some(0)
        );
    }
}

#[test]
fn object_transform_updates_other_translation_units_references_to_main() {
    let root = tempfile::tempdir().unwrap();
    let files = [
        ("original.c", "int main(void){return 42;}"),
        (
            "caller.c",
            "int main(void); int call_original(void){return main();}",
        ),
        (
            "driver.c",
            "int call_original(void); int main(void){return call_original()!=42;}",
        ),
    ];
    for (name, source) in files {
        std::fs::write(root.path().join(name), source).unwrap();
    }
    std::fs::write(root.path().join("otelc.toml"),"schema_version=2\nlanguages=['c']\n[sources]\ninclude=['original.c','caller.c']\n[functions]\ninclude=['call_original']\n").unwrap();
    for name in ["original", "caller"] {
        let input = format!("{name}.o");
        let output = format!("{name}-renamed.o");
        success(cli(
            &["clang", "-O0", "-c", &format!("{name}.c"), "-o", &input],
            root.path(),
        ));
        success(transform(root.path(), &input, &output, "renamed_main"));
    }
    success(
        Command::new(tool("clang"))
            .args(["-c", "driver.c", "-o", "driver.o"])
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    success(cli(
        &[
            "clang",
            "original-renamed.o",
            "caller-renamed.o",
            "driver.o",
            "-o",
            "app",
        ],
        root.path(),
    ));
    success(
        Command::new(root.path().join("app"))
            .env_remove("OTELC_CONFIG")
            .env_remove("OTELC_MANIFEST")
            .output()
            .unwrap(),
    );
    for (name, source) in files {
        assert_eq!(
            std::fs::read_to_string(root.path().join(name)).unwrap(),
            source
        );
    }
}

#[test]
fn object_transform_help_is_successful_and_does_not_require_config_or_toolchain() {
    let root = tempfile::tempdir().unwrap();
    for arguments in [
        vec!["object", "--help"],
        vec!["object", "rename-main", "--help"],
    ] {
        let result = success(cli(&arguments, root.path()));
        assert!(result.stderr.is_empty());
        let help = String::from_utf8(result.stdout).unwrap();
        for option in ["rename-main", "INPUT.o", "--output", "--symbol"] {
            assert!(help.contains(option), "{help}");
        }
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 0);
    }
    assert!(!cli(
        &["object", "rename-main", "missing.o", "--help"],
        root.path()
    )
    .status
    .success());
    assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 0);
}
