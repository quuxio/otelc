mod compiler;
mod control;
mod managed;
use anyhow::{bail, Context, Result};
use quux_otelc_config::{
    common::{CommonConfig, Language},
    Config,
};
use quux_otelc_symbols::{manifest_path, Manifest};
use std::{
    path::{Path, PathBuf},
    process::Command,
};
fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("quux-otelc: {error:#}");
            std::process::exit(1);
        }
    }
}
fn run() -> Result<i32> {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || matches!(args[0].as_str(), "--help" | "-h") {
        println!("quux-otelc [--config PATH] [--language ID] status|enable|disable --socket PATH | config [--json] [--require-supported]|doctor|inspect BINARY [--all|--json]|run BINARY [ARGS...]|clang [ARGS...]|clang++ [ARGS...]\nNative function timing and interim opt-in lifetime metrics. Python 3.12+ function timing is available with python SCRIPT or python -m MODULE; JavaScript function timing is available with node SCRIPT; TypeScript function timing is available with ts SCRIPT; Java function timing is available with java CLASS, SOURCE.java or -jar JAR; Go function timing is available with go SOURCE.go or PACKAGE; Rust remains planned. LLVM backend supports C++ exceptions; callbacks require existing -fno-exceptions.\nBuild the CLI and static runtime together with make build.");
        return Ok(0);
    }
    let mut config_path = PathBuf::from("otelc.toml");
    let mut language = None;
    while args
        .first()
        .is_some_and(|a| a == "--config" || a == "--language")
    {
        if args.len() < 3 {
            bail!("global options require a value and command");
        }
        let option = args.remove(0);
        let value = args.remove(0);
        if option == "--config" {
            config_path = PathBuf::from(value);
        } else {
            language = Some(value.parse::<Language>()?);
        }
    }
    if let Some(language) = language {
        std::env::set_var("OTELC_LANGUAGE", language.to_string());
    }
    let command = args.remove(0);
    match command.as_str() {
        "status" | "enable" | "disable" => control::run(&command, &args),
        "config" => {
            if args
                .iter()
                .any(|a| a != "--json" && a != "--require-supported")
            {
                bail!("unknown config option");
            }
            let language = language.context("config requires --language")?;
            let resolved = CommonConfig::load(&config_path, true)?.resolve(language)?;
            if args.iter().any(|a| a == "--json") {
                println!("{}", serde_json::to_string_pretty(&resolved)?);
            } else {
                println!("Common schema 2: {language}; backend {}", resolved.backend);
                println!("Execution available: {}", resolved.execution_available);
                for reason in &resolved.unavailable {
                    println!("Unavailable: {reason}");
                }
            }
            if args.iter().any(|a| a == "--require-supported") && !resolved.execution_available {
                bail!("requested language/capabilities are not executable");
            }
            Ok(0)
        }
        "python" | "node" | "ts" | "java" | "go" => {
            managed::run(&command, &args, &config_path, language)
        }
        "doctor" if language == Some(Language::Go) => {
            managed::run("go", &["--doctor".into()], &config_path, language)
        }
        "inspect" if language == Some(Language::Go) => {
            let mut options = vec!["--inspect".into()];
            options.extend(args);
            managed::run("go", &options, &config_path, language)
        }
        "doctor" if language == Some(Language::Java) => {
            managed::run("java", &["--doctor".into()], &config_path, language)
        }
        "inspect" if language == Some(Language::Java) => {
            let mut options = vec!["--inspect".into()];
            options.extend(args);
            managed::run("java", &options, &config_path, language)
        }
        "doctor" if matches!(language, Some(Language::JavaScript | Language::TypeScript)) => {
            let command = if language == Some(Language::TypeScript) {
                "ts"
            } else {
                "node"
            };
            managed::run(command, &["--doctor".into()], &config_path, language)
        }
        "inspect" if matches!(language, Some(Language::JavaScript | Language::TypeScript)) => {
            let mut options = vec!["--inspect".into()];
            options.extend(args);
            let command = if language == Some(Language::TypeScript) {
                "ts"
            } else {
                "node"
            };
            managed::run(command, &options, &config_path, language)
        }
        "doctor" if language == Some(Language::Python) => {
            managed::run("python", &["--doctor".into()], &config_path, language)
        }
        "doctor" => {
            if !args.is_empty() {
                bail!("doctor takes no arguments");
            }
            let config = if config_path.exists() {
                Some(Config::load(&config_path, true)?)
            } else {
                None
            };
            let llvm = if config.as_ref().is_some_and(|c| c.build.backend == "llvm") {
                Some(compiler::llvm_toolchain()?)
            } else {
                None
            };
            let clang = llvm
                .as_ref()
                .map(|toolchain| toolchain.bindir.join("clang"))
                .unwrap_or_else(|| PathBuf::from("clang"));
            let output = Command::new(&clang)
                .arg("--version")
                .output()
                .context("find Clang")?;
            println!(
                "quux-otelc {} — native timing prototype",
                env!("CARGO_PKG_VERSION")
            );
            println!(
                "Host: {} / {}",
                std::env::consts::OS,
                std::env::consts::ARCH
            );
            println!("{}", String::from_utf8_lossy(&output.stdout));
            println!("Runtime: {}", compiler::runtime_path()?.display());
            println!("macOS ARM64: local qualification target; other hosts require native qualification\nLLVM 22: C++ normal returns and Itanium exception unwinding\nCallbacks: C normal returns; C++ requires existing -fno-exceptions\nExisting function annotations: LLVM with annotations.read_existing\nLive metrics control: LLVM with runtime.control_socket\nObject lifetime metrics: explicit C++ guard\nSpans, LTO, sanitizers, shared libraries and async: unsupported");
            if let Some(config) = config {
                println!("Backend: {}", config.build.backend);
                println!(
                    "Configured stack/queue pools: {} bytes",
                    config.memory_bytes()?
                );
            }
            Ok(if output.status.success() { 0 } else { 1 })
        }
        "inspect" if language == Some(Language::Python) => {
            let mut options = vec!["--inspect".into()];
            options.extend(args);
            managed::run("python", &options, &config_path, language)
        }
        "inspect" => {
            let binary = Path::new(args.first().context("inspect requires an executable")?);
            if args.iter().skip(1).any(|a| a != "--all" && a != "--json") {
                bail!("unknown inspect option");
            }
            let manifest = Manifest::load(&manifest_path(binary))?;
            manifest.verify(binary)?;
            if args.iter().any(|a| a == "--json") {
                println!("{}", serde_json::to_string_pretty(&manifest)?);
            } else {
                println!(
                    "Image: {} ({})\nCompiler: {}\nSelected: {} / {} functions",
                    manifest.image_id,
                    manifest.architecture,
                    manifest.compiler,
                    manifest.functions.iter().filter(|f| f.selected).count(),
                    manifest.functions.len()
                );
                for function in manifest
                    .functions
                    .iter()
                    .filter(|f| f.selected || args.iter().any(|a| a == "--all"))
                {
                    println!(
                        "{}  {} ({})",
                        if function.selected {
                            "included"
                        } else {
                            "excluded"
                        },
                        function.display_name,
                        function.reason
                    );
                }
            }
            Ok(0)
        }
        "run" => {
            let binary = PathBuf::from(args.first().context("run requires an executable")?)
                .canonicalize()?;
            let config = Config::load(&config_path, true)?;
            let manifest = Manifest::load(&manifest_path(&binary))?;
            manifest.verify(&binary)?;
            if manifest.functions.iter().filter(|f| f.selected).count()
                > config.runtime.max_functions
            {
                bail!("manifest exceeds max_functions");
            }
            let mut child = Command::new(&binary);
            child
                .args(&args[1..])
                .env("OTELC_CONFIG", config_path.canonicalize()?)
                .env("OTELC_MANIFEST", manifest_path(&binary));
            #[cfg(unix)]
            {
                use std::os::unix::process::CommandExt;
                Err(child.exec().into())
            }
            #[cfg(not(unix))]
            {
                bail!("only Unix platforms are supported");
            }
        }
        _ => compiler::wrap(&command, &args, &config_path),
    }
}
