use crate::compiler_profile::{HookEvidence, Toolchain};
use anyhow::{bail, Context, Result};
use quux_otelc_rust::policy::Plan;
use std::{
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

pub(crate) const BASELINE_FLAGS: [&str; 3] = ["--edition=2024", "-O", "-g"];
pub(crate) struct Compilation {
    pub binary: PathBuf,
    pub argv: Vec<String>,
    pub baseline_flags: Vec<String>,
    pub hook: Option<HookEvidence>,
    pub inventory: Option<PathBuf>,
    pub profile: Option<Vec<u8>>,
    pub resolved: Option<PathBuf>,
    pub mir_hook: Option<crate::compiler_mir::HookEvidence>,
    pub request: Option<PathBuf>,
}

pub(crate) fn compile(
    plan: &Plan,
    filename: &Path,
    root: &Path,
    scratch: &Path,
    tools: &Toolchain,
) -> Result<PathBuf> {
    Ok(compile_recorded(plan, filename, root, scratch, tools)?.binary)
}

pub(crate) fn compile_recorded(
    plan: &Plan,
    filename: &Path,
    root: &Path,
    scratch: &Path,
    tools: &Toolchain,
) -> Result<Compilation> {
    if tools.llvm.is_some() {
        return crate::compiler_llvm::compile_recorded(plan, filename, root, scratch, tools);
    }
    if tools.mir.is_some() {
        return crate::compiler_mir::compile_recorded(plan, filename, root, scratch, tools);
    }
    bail!("Rust compilation requires a qualified compiler profile");
}

pub fn wrapper(args: Vec<OsString>) -> Result<i32> {
    let compiler = args
        .first()
        .context("Cargo compiler wrapper requires rustc")?;
    let options = args[1..].to_vec();
    let root =
        PathBuf::from(std::env::var_os("OTELC_RUST_ROOT").context("Rust wrapper project root")?);
    if let Some(index) = options.iter().position(|value| {
        Path::new(value)
            .extension()
            .is_some_and(|extension| extension == "rs")
    }) {
        let original = PathBuf::from(&options[index]);
        let absolute = original.canonicalize()?;
        // Build outputs already use a private CARGO_TARGET_DIR. A directory
        // named target in an original source path is not a build-script marker.
        if absolute.starts_with(&root)
            && !options
                .windows(2)
                .any(|values| values[0] == "--crate-name" && values[1] == "build_script_build")
        {
            if options.iter().any(|value| {
                value == "--test" || value == "proc-macro" || value == "--edition=2015"
            }) {
                bail!("Rust test/proc-macro/2015 crates require separate qualification");
            }
            let plan = Plan::load(Path::new(
                &std::env::var_os("OTELC_RUST_PLAN").context("Rust wrapper policy")?,
            ))?;
            let adapter = std::env::current_exe()?;
            let tools = Toolchain::load(&plan.backend, &adapter)?;
            let profile = tools
                .mir
                .as_ref()
                .context("Cargo requires the stable Rust compiler profile")?;
            // Cargo sets RUSTC_WRAPPER to this adapter. Validate the explicit
            // compiler argument without treating our own wrapper as foreign.
            profile.doctor(Path::new(compiler))?;
            let scratch = tempfile::tempdir()?;
            crate::compiler_mir::compile_arguments(
                &plan,
                &original,
                &root,
                scratch.path(),
                &tools,
                options,
            )?;
            return Ok(0);
        }
    }
    Ok(quux_otelc_process::child_code(
        Command::new(compiler).args(options).status()?,
    )?)
}
pub fn run(plan_path: &Path, args: &[String], adapter: &Path) -> Result<i32> {
    let plan = Plan::load(plan_path)?;
    if args.first().is_some_and(|arg| arg == "--run") {
        let directory = args
            .get(1)
            .context("Rust artifact launch requires --run DIRECTORY [ARGS...]")?;
        return crate::artifact::launch(plan_path, Path::new(directory), &args[2..], adapter);
    }
    let root = std::env::current_dir()?.canonicalize()?;
    let tools = Toolchain::load(&plan.backend, adapter)?;
    if args == ["--doctor"] {
        tools.doctor()?;
        if tools.llvm.is_some() {
            println!("Rust qualified nightly/LLVM 23: original-source synchronous compiler hooks; async and Cargo execution unqualified");
        } else {
            println!("Rust 1.98.1: original-source compiler probes, Cargo wrapper and SDK metrics available");
        }
        return Ok(0);
    }
    if args.first().is_some_and(|value| value == "--inspect") {
        if !(2..=3).contains(&args.len()) || args.get(2).is_some_and(|value| value != "--json") {
            bail!("Rust inspect requires SOURCE.rs [--json]");
        }
        tools.doctor()?;
        let scratch = tempfile::tempdir()?;
        let compilation =
            compile_recorded(&plan, Path::new(&args[1]), &root, scratch.path(), &tools)?;
        let inventory = compilation
            .inventory
            .context("Rust inspection requires the compiled inventory")?;
        let mut result: serde_json::Value = serde_json::from_slice(&fs::read(inventory)?)?;
        result["language"] = "rust".into();
        result["backend"] = plan.backend.clone().into();
        result["phase"] = "compiled".into();
        println!("{}", serde_json::to_string_pretty(&result)?);
        return Ok(0);
    }
    if args.first().is_some_and(|arg| arg == "--build") {
        if args.len() != 4 || args[2] != "--output-dir" || !args[1].ends_with(".rs") {
            bail!("Rust build-only requires --build SOURCE.rs --output-dir DIRECTORY; Cargo build-only is not yet qualified");
        }
        tools.doctor()?;
        crate::artifact::build(
            plan_path,
            Path::new(&args[1]),
            Path::new(&args[3]),
            &root,
            &tools,
            adapter,
        )?;
        return Ok(0);
    }
    let target = args
        .first()
        .context("rust requires SOURCE.rs or Cargo.toml [ARGS...]")?;
    tools.doctor()?;
    let scratch = tempfile::tempdir()?;
    let (binary, application_args) = if target.ends_with(".rs") {
        (
            compile(&plan, Path::new(target), &root, scratch.path(), &tools)?,
            args[1..].to_vec(),
        )
    } else if target.ends_with("Cargo.toml") {
        if tools.llvm.is_some() {
            bail!("Rust LLVM Cargo integration requires separate qualification; use a standalone source file");
        }
        let manifest = PathBuf::from(target).canonicalize()?;
        if !manifest.starts_with(&root) {
            bail!("Rust Cargo manifest is outside the project root; run from the Cargo project directory");
        }
        if !manifest
            .parent()
            .context("Cargo manifest directory")?
            .join("Cargo.lock")
            .is_file()
        {
            bail!("Rust Cargo execution requires an existing lockfile");
        }
        let mut command = Command::new("cargo");
        command
            .args([
                "build",
                "--locked",
                "--message-format=json",
                "--manifest-path",
            ])
            .arg(&manifest)
            .env("CARGO_TARGET_DIR", scratch.path().join("cargo-target"))
            .env("CARGO_INCREMENTAL", "0")
            .env("RUSTC", &tools.rustc)
            .env("RUSTC_WRAPPER", adapter)
            .env("OTELC_RUST_ROOT", &root)
            .env("OTELC_RUST_PLAN", plan_path)
            .stderr(Stdio::inherit());
        let mut index = 1;
        if args.get(index).is_some_and(|value| value == "--bin") {
            let name = args.get(index + 1).context("--bin requires NAME")?;
            command.args(["--bin", name]);
            index += 2;
        }
        if args.get(index).is_some_and(|value| value == "--") {
            index += 1;
        }
        let output = command.output()?;
        if !output.status.success() {
            bail!("Cargo instrumentation build failed");
        }
        let binaries: Vec<PathBuf> = String::from_utf8_lossy(&output.stdout)
            .lines()
            .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
            .filter(|value| {
                value["reason"] == "compiler-artifact"
                    && value["target"]["kind"]
                        .as_array()
                        .is_some_and(|kinds| kinds.iter().any(|kind| kind == "bin"))
            })
            .filter_map(|value| value["executable"].as_str().map(PathBuf::from))
            .collect();
        if binaries.len() != 1 {
            bail!("Rust Cargo launch requires one executable; select --bin NAME");
        }
        (binaries[0].clone(), args[index..].to_vec())
    } else {
        bail!("rust requires SOURCE.rs or Cargo.toml [ARGS...]");
    };
    if let Some(directory) = std::env::var_os("OTELC_COVERAGE_BIN_DIR") {
        fs::create_dir_all(&directory)?;
        fs::copy(
            &binary,
            PathBuf::from(directory).join(format!("rust-sdk-{}", std::process::id())),
        )?;
    }
    Ok(quux_otelc_process::child_code(
        Command::new(binary)
            .args(application_args)
            .env("OTELC_RUST_PLAN", plan_path)
            .status()?,
    )?)
}
