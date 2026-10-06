//! Conservative driver classification; unsupported builds fail before compilation.
use anyhow::{bail, Context, Result};
use quux_otelc_config::Config;
use quux_otelc_symbols::{create, mark_object, object_probes, Probes};
use std::{
    path::{Path, PathBuf},
    process::Command,
};
pub fn runtime_path() -> Result<PathBuf> {
    let path = std::env::current_exe()?
        .parent()
        .context("CLI directory")?
        .join("libquux_otelc_runtime.a");
    if !path.is_file() {
        bail!(
            "runtime archive missing at {}; build the workspace first",
            path.display()
        );
    }
    Ok(path)
}
fn expanded(args: &[String], depth: u32) -> Result<Vec<String>> {
    if depth > 8 {
        bail!("response file nesting exceeds 8");
    }
    let mut result = Vec::new();
    for argument in args {
        if let Some(path) = argument.strip_prefix('@') {
            let text = std::fs::read_to_string(path).context("read compiler response file")?;
            if text.len() > 1024 * 1024 {
                bail!("response file exceeds 1 MiB");
            }
            result.extend(expanded(
                &shell_words::split(&text).context("parse compiler response file")?,
                depth + 1,
            )?);
        } else {
            result.push(argument.clone());
        }
    }
    Ok(result)
}
fn unsupported(args: &[String]) -> Result<()> {
    for argument in args {
        if argument.starts_with("-flto")
            || argument.starts_with("-fsanitize")
            || argument.starts_with("-finstrument")
            || argument.starts_with("-fprofile")
            || argument.starts_with("-fcoverage")
            || matches!(
                argument.as_str(),
                "-shared"
                    | "-dynamiclib"
                    | "-bundle"
                    | "-m32"
                    | "-emit-llvm"
                    | "-x"
                    | "-Xclang"
                    | "-fuse-ld=lld"
            )
            || argument.starts_with("--target")
            || argument == "-target"
            || argument.starts_with("-fpass-plugin")
            || argument.starts_with("-Wl,--icf")
        {
            bail!("unsupported instrumentation option: {argument}");
        }
    }
    Ok(())
}
fn inputs(args: &[String]) -> Vec<&String> {
    let mut result = Vec::new();
    let mut skip = false;
    for argument in args {
        if skip {
            skip = false;
            continue;
        }
        if matches!(
            argument.as_str(),
            "-o" | "-I"
                | "-isystem"
                | "-include"
                | "-imacros"
                | "-MF"
                | "-MT"
                | "-MQ"
                | "-isysroot"
                | "-arch"
                | "-Xlinker"
                | "-L"
                | "-D"
                | "-U"
                | "-B"
        ) {
            skip = true;
            continue;
        }
        if !argument.starts_with('-') {
            result.push(argument);
        }
    }
    result
}
fn source(argument: &str) -> bool {
    matches!(
        Path::new(argument).extension().and_then(|s| s.to_str()),
        Some("c" | "cpp" | "cc" | "cxx" | "C")
    )
}
fn compiler_code(status: std::process::ExitStatus) -> i32 {
    status.code().unwrap_or_else(|| {
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            128 + status.signal().unwrap_or(1)
        }
        #[cfg(not(unix))]
        {
            1
        }
    })
}
pub fn wrap(compiler: &str, args: &[String], config_path: &Path) -> Result<i32> {
    let name = Path::new(compiler)
        .file_name()
        .and_then(|v| v.to_str())
        .unwrap_or("");
    if !matches!(name, "clang" | "clang++") {
        bail!("expected doctor, inspect, run, clang or clang++");
    }
    let args = expanded(args, 0)?;
    let pass_through = args.is_empty()
        || args.iter().any(|a| {
            matches!(
                a.as_str(),
                "-E" | "-M"
                    | "-MM"
                    | "-S"
                    | "-fsyntax-only"
                    | "--version"
                    | "-dumpmachine"
                    | "-dumpversion"
                    | "-print-search-dirs"
                    | "-###"
            )
        });
    if pass_through {
        return Ok(compiler_code(Command::new(compiler).args(&args).status()?));
    }
    unsupported(&args)?;
    let input_files = inputs(&args);
    let sources: Vec<_> = input_files.iter().copied().filter(|a| source(a)).collect();
    let cpp = sources.iter().any(|s| !s.ends_with(".c")) || name == "clang++";
    let language = if cpp {
        quux_otelc_config::common::Language::Cpp
    } else {
        quux_otelc_config::common::Language::C
    };
    if let Ok(value) = std::env::var("OTELC_LANGUAGE") {
        if value.parse::<quux_otelc_config::common::Language>()? != language {
            bail!("selected language does not match the compiler invocation");
        }
    }
    let config = Config::load_for_language(config_path, false, Some(language))?;
    let llvm = if config.build.backend == "llvm" {
        Some(llvm_toolchain()?)
    } else {
        None
    };
    let compiler_path = llvm.as_ref().map(|toolchain| toolchain.bindir.join(name));
    let compiler = compiler_path
        .as_deref()
        .unwrap_or_else(|| Path::new(compiler));
    let selection = config.path_selection()?;
    let cwd = std::env::current_dir()?;
    let selected: Vec<_> = sources
        .iter()
        .map(|path| {
            let path = Path::new(path);
            let normalized = if path.is_absolute() {
                path.strip_prefix(&cwd).unwrap_or(path)
            } else {
                path
            };
            selection.accepts(&normalized.to_string_lossy().replace('\\', "/"))
        })
        .collect();
    if selected.iter().any(|v| *v) && selected.iter().any(|v| !*v) {
        bail!("mixed selected/unselected source files require separate compile invocations");
    }
    let instrument = selected.iter().any(|v| *v);
    if instrument && cpp && config.build.backend == "callbacks" {
        let exceptions = args
            .iter()
            .rev()
            .find(|a| a.as_str() == "-fexceptions" || a.as_str() == "-fno-exceptions");
        if exceptions.map(|v| v.as_str()) != Some("-fno-exceptions") {
            bail!("callback timing requires existing -fno-exceptions for selected C++");
        }
    }
    let compile_only = args.iter().any(|a| a == "-c");
    let output = if let Some(index) = args.iter().position(|a| a == "-o") {
        PathBuf::from(args.get(index + 1).context("-o requires a path")?)
    } else if let Some(value) = args
        .iter()
        .find_map(|a| a.strip_prefix("-o").filter(|v| !v.is_empty()))
    {
        PathBuf::from(value)
    } else if compile_only {
        if sources.len() != 1 {
            bail!("compile-only wrapping requires one source or explicit -o");
        }
        PathBuf::from(sources[0])
            .file_name()
            .map(PathBuf::from)
            .context("source filename")?
            .with_extension("o")
    } else {
        PathBuf::from("a.out")
    };
    let mut probed = Probes::default();
    for input in input_files.iter().filter(|a| a.ends_with(".o")) {
        probed.extend(object_probes(Path::new(input), &config.build.backend)?);
    }
    let scratch = if !compile_only && !sources.is_empty() {
        Some(tempfile::tempdir()?)
    } else {
        None
    };
    let linked_output = scratch
        .as_ref()
        .map(|d| d.path().join("app"))
        .unwrap_or_else(|| output.clone());
    let mut command = Command::new(compiler);
    command.args(&args);
    if scratch.is_some() {
        command.arg("-save-temps=obj").arg("-o").arg(&linked_output);
    }
    if instrument {
        if let Some(toolchain) = &llvm {
            command.arg(format!("-fpass-plugin={}", toolchain.plugin.display()));
            command.env(
                "OTELC_FUNCTION_INCLUDE",
                serde_json::to_string(&config.functions.include)?,
            );
            command.env(
                "OTELC_FUNCTION_EXCLUDE",
                serde_json::to_string(&config.functions.exclude)?,
            );
            command.env(
                "OTELC_READ_ANNOTATIONS",
                if config.read_annotations { "1" } else { "0" },
            );
            if cpp {
                command.env("OTELC_LLVM_CPP", "1");
            } else {
                command.env_remove("OTELC_LLVM_CPP");
            }
        } else {
            command.arg("-finstrument-functions");
        }
    }
    if std::env::var_os("CARGO_LLVM_COV").is_some() {
        command.args(["-fprofile-instr-generate", "-fcoverage-mapping"]);
    }
    if !compile_only {
        command.arg(runtime_path()?);
        // Coverage builds need Clang to link the profiler runtime for Rust's
        // instrumented static archive. Ordinary application builds omit this.
        if std::env::var_os("CARGO_LLVM_COV").is_some() {
            let library = if cfg!(target_os = "macos") {
                "libclang_rt.profile_osx.a"
            } else if cfg!(target_arch = "aarch64") {
                "libclang_rt.profile-aarch64.a"
            } else {
                "libclang_rt.profile-x86_64.a"
            };
            let output = Command::new(compiler)
                .arg(format!("-print-file-name={library}"))
                .output()?;
            let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !Path::new(&path).is_file() {
                bail!("matching native profiler runtime is unavailable");
            }
            command.arg(path);
        }
        #[cfg(target_os = "macos")]
        {
            command.args([
                "-Wl,-u,_otelc_initialize",
                "-Wl,-u,___cyg_profile_func_enter",
                "-Wl,-u,_otelc_function_enter_v1",
                "-framework",
                "Security",
                "-framework",
                "CoreFoundation",
                "-liconv",
                "-lresolv",
            ]);
        }
        #[cfg(target_os = "linux")]
        {
            command.args([
                "-Wl,-u,otelc_initialize",
                "-Wl,-u,__cyg_profile_func_enter",
                "-Wl,-u,otelc_function_enter_v1",
                "-Wl,--build-id",
                "-ldl",
                "-lm",
            ]);
        }
        command.arg("-lpthread");
    }
    let status = command.status()?;
    if !status.success() {
        return Ok(compiler_code(status));
    }
    if compile_only {
        mark_object(&output, instrument, &config.build.backend)?;
    }
    if let Some(scratch) = &scratch {
        for entry in std::fs::read_dir(scratch.path())? {
            let path = entry?.path();
            if path.extension().is_some_and(|e| e == "o") {
                probed.extend(mark_object(&path, instrument, &config.build.backend)?);
            }
        }
        if linked_output != output {
            std::fs::copy(&linked_output, &output)?;
        }
    }
    if !compile_only {
        let version = Command::new(compiler).arg("--version").output()?;
        let version = String::from_utf8_lossy(&version.stdout)
            .lines()
            .next()
            .unwrap_or("unknown Clang")
            .to_string();
        create(&output, &config, version, &probed)?;
    }
    Ok(0)
}
#[cfg(test)]
#[path = "tests/compiler.rs"]
mod tests;

#[derive(Debug)]
pub struct LlvmToolchain {
    pub bindir: PathBuf,
    pub plugin: PathBuf,
}
pub fn llvm_toolchain() -> Result<LlvmToolchain> {
    let executable = std::env::current_exe()?;
    let directory = executable.parent().context("CLI directory")?;
    let metadata: serde_json::Value = serde_json::from_slice(
        &std::fs::read(directory.join("otelc-llvm-toolchain.json"))
            .context("LLVM backend missing; run make build with LLVM 22 installed")?,
    )?;
    let bindir = PathBuf::from(
        metadata["bindir"]
            .as_str()
            .context("LLVM compiler directory")?,
    );
    let plugin = directory.join(metadata["plugin"].as_str().context("LLVM pass path")?);
    let version = Command::new(bindir.join("clang"))
        .arg("--version")
        .output()?;
    if !version.status.success()
        || !String::from_utf8_lossy(&version.stdout).contains(&format!(
            "clang version {}",
            metadata["version"].as_str().context("LLVM version")?
        ))
        || !plugin.is_file()
    {
        bail!("LLVM compiler/pass version mismatch; rebuild the backend");
    }
    Ok(LlvmToolchain { bindir, plugin })
}
