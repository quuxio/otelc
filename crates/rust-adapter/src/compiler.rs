use anyhow::{bail, Context, Result};
use quux_otelc_config::Selection;
use quux_otelc_rust::policy::Plan;
use std::{
    ffi::OsString,
    fs,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

pub fn doctor(rustc: &str) -> Result<()> {
    let output = Command::new(rustc)
        .arg("--version")
        .output()
        .context("locate rustc")?;
    if !output.status.success()
        || !String::from_utf8_lossy(&output.stdout).starts_with("rustc 1.98.1 ")
    {
        bail!("Rust adapter requires the qualified rustc 1.98.1 toolchain");
    }
    if std::env::var_os("RUSTC_WRAPPER").is_some()
        || std::env::var_os("RUSTC_WORKSPACE_WRAPPER").is_some()
    {
        bail!("existing Rust compiler wrappers require separate qualification");
    }
    Ok(())
}
pub fn mirror(root: &Path, destination: &Path) -> Result<()> {
    fs::create_dir_all(destination)?;
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let source = entry.path();
        let name = entry.file_name();
        let target = destination.join(&name);
        let kind = entry.file_type()?;
        if kind.is_dir()
            && matches!(
                name.to_str(),
                Some(".git" | "target" | "node_modules" | ".venv")
            )
        {
            symlink(source, target)?;
        } else if kind.is_dir() {
            mirror(&source, &target)?;
        } else if kind.is_file() && source.extension().is_some_and(|value| value == "rs") {
            fs::copy(source, target)?;
        } else {
            symlink(source, target)?;
        }
    }
    Ok(())
}
fn prepare(
    root: &Path,
    generated: &Path,
    filename: &Path,
    plan: &Plan,
    startup: bool,
) -> Result<PathBuf> {
    let relative = filename
        .strip_prefix(root)
        .context("Rust source is outside the project root")?;
    let identity = relative
        .with_extension("")
        .to_string_lossy()
        .replace(['/', '\\'], ".");
    let selection = Selection::new(&plan.sources.include, &plan.sources.exclude, true)?;
    let original = fs::read_to_string(filename)?;
    let (source, _) = crate::transform::transform(
        &original,
        &identity,
        plan,
        selection.accepts(&relative.to_string_lossy().replace('\\', "/")),
        startup,
        false,
    )?;
    let target = generated.join(relative);
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(&target, source)?;
    Ok(target)
}
fn source_tree(root: &Path, generated: &Path, plan: &Plan, main: &Path) -> Result<()> {
    fn walk(root: &Path, current: &Path, generated: &Path, plan: &Plan, main: &Path) -> Result<()> {
        for entry in fs::read_dir(current)? {
            let entry = entry?;
            let source = entry.path();
            let kind = entry.file_type()?;
            if kind.is_dir() {
                walk(root, &source, generated, plan, main)?;
            } else if kind.is_file() && source.extension().is_some_and(|value| value == "rs") {
                let relative = source.strip_prefix(generated)?;
                let selection = Selection::new(&plan.sources.include, &plan.sources.exclude, true)?;
                if !selection.accepts(&relative.to_string_lossy()) && root.join(relative) != main {
                    continue;
                }
                prepare(
                    root,
                    generated,
                    &root.join(relative),
                    plan,
                    root.join(relative) == main,
                )?;
            }
        }
        Ok(())
    }
    walk(root, generated, generated, plan, main)
}
fn sdk_arguments(
    args: &mut Vec<OsString>,
    sdk: &Path,
    source: &Path,
    original: &Path,
) -> Result<()> {
    if args
        .iter()
        .any(|arg| arg.to_string_lossy().starts_with("quux_otelc_rust="))
    {
        bail!("application dependency collides with the Rust probe crate");
    }
    let deps = sdk.parent().context("Rust SDK directory")?.join("deps");
    args.extend([
        "--extern".into(),
        format!("quux_otelc_rust={}", sdk.display()).into(),
        "-L".into(),
        format!("dependency={}", deps.display()).into(),
    ]);
    let generated_parent = source.parent().context("generated source parent")?;
    let original_parent = original.parent().unwrap_or(Path::new(""));
    args.push(
        format!(
            "--remap-path-prefix={}={}",
            generated_parent.display(),
            original_parent.display()
        )
        .into(),
    );
    Ok(())
}
pub fn compile(
    plan: &Plan,
    filename: &Path,
    root: &Path,
    scratch: &Path,
    sdk: &Path,
    rustc: &str,
) -> Result<PathBuf> {
    let original_argument = filename.to_path_buf();
    let filename = filename.canonicalize()?;
    let generated = scratch.join("source");
    mirror(root, &generated)?;
    source_tree(root, &generated, plan, &filename)?;
    let source = generated.join(filename.strip_prefix(root)?);
    let binary = scratch.join("application");
    let mut args = vec![
        source.as_os_str().to_owned(),
        "--edition=2024".into(),
        "-O".into(),
        "-g".into(),
        "-o".into(),
        binary.as_os_str().to_owned(),
    ];
    sdk_arguments(&mut args, sdk, &source, &original_argument)?;
    let status = Command::new(rustc)
        .args(args)
        .status()
        .context("compile generated Rust input")?;
    if !status.success() {
        bail!("Rust compilation failed");
    }
    Ok(binary)
}
pub fn wrapper(args: Vec<OsString>) -> Result<i32> {
    let compiler = args
        .first()
        .context("Cargo compiler wrapper requires rustc")?;
    let mut options = args[1..].to_vec();
    let root =
        PathBuf::from(std::env::var_os("OTELC_RUST_ROOT").context("Rust wrapper project root")?);
    let generated = PathBuf::from(
        std::env::var_os("OTELC_RUST_GENERATED").context("Rust wrapper generated directory")?,
    );
    if let Some(index) = options.iter().position(|value| {
        Path::new(value)
            .extension()
            .is_some_and(|extension| extension == "rs")
    }) {
        let original = PathBuf::from(&options[index]);
        let absolute = original.canonicalize()?;
        if absolute.starts_with(&root)
            && !absolute
                .components()
                .any(|part| part.as_os_str() == "target")
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
            let startup = options
                .windows(2)
                .any(|values| values[0] == "--crate-type" && values[1] == "bin");
            let source = prepare(&root, &generated, &absolute, &plan, startup)?;
            options[index] = source.as_os_str().to_owned();
            let sdk =
                PathBuf::from(std::env::var_os("OTELC_RUST_SDK").context("Rust wrapper SDK")?);
            sdk_arguments(&mut options, &sdk, &source, &original)?;
        }
    }
    Ok(Command::new(compiler)
        .args(options)
        .status()?
        .code()
        .unwrap_or(1))
}
pub fn run(plan_path: &Path, args: &[String], adapter: &Path) -> Result<i32> {
    let plan = Plan::load(plan_path)?;
    let root = std::env::current_dir()?.canonicalize()?;
    let sdk = adapter
        .parent()
        .context("Rust adapter directory")?
        .join("libquux_otelc_rust.rlib");
    if !sdk.is_file() {
        bail!("Rust SDK rlib is missing; run make build");
    }
    let rustc = std::env::var("OTELC_RUSTC").unwrap_or_else(|_| "rustc".into());
    if args == ["--doctor"] {
        doctor(&rustc)?;
        println!("Rust 1.98.1: generated body guards, Cargo wrapper and SDK metrics available");
        return Ok(0);
    }
    if args.first().is_some_and(|value| value == "--inspect") {
        if !(2..=3).contains(&args.len()) || args.get(2).is_some_and(|value| value != "--json") {
            bail!("Rust inspect requires SOURCE.rs [--json]");
        }
        let file = PathBuf::from(&args[1]).canonicalize()?;
        let relative = file.strip_prefix(&root)?;
        let selection = Selection::new(&plan.sources.include, &plan.sources.exclude, true)?;
        let (_, functions) = crate::transform::transform(
            &fs::read_to_string(file.clone())?,
            &relative
                .with_extension("")
                .to_string_lossy()
                .replace(['/', '\\'], "."),
            &plan,
            selection.accepts(&relative.to_string_lossy()),
            false,
            true,
        )?;
        println!(
            "{}",
            serde_json::to_string_pretty(
                &serde_json::json!({"language":"rust","functions":functions})
            )?
        );
        return Ok(0);
    }
    let target = args
        .first()
        .context("rust requires SOURCE.rs or Cargo.toml [ARGS...]")?;
    doctor(&rustc)?;
    let scratch = tempfile::tempdir()?;
    let (binary, application_args) = if target.ends_with(".rs") {
        (
            compile(
                &plan,
                Path::new(target),
                &root,
                scratch.path(),
                &sdk,
                &rustc,
            )?,
            args[1..].to_vec(),
        )
    } else if target.ends_with("Cargo.toml") {
        let manifest = PathBuf::from(target).canonicalize()?;
        if !manifest
            .parent()
            .context("Cargo manifest directory")?
            .join("Cargo.lock")
            .is_file()
        {
            bail!("Rust Cargo execution requires an existing lockfile");
        }
        let generated = scratch.path().join("source");
        mirror(&root, &generated)?;
        // Compile every mirrored source before Cargo reads generated module paths.
        source_tree(&root, &generated, &plan, Path::new(""))?;
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
            .env("RUSTC_WRAPPER", adapter)
            .env("OTELC_RUST_ROOT", &root)
            .env("OTELC_RUST_GENERATED", &generated)
            .env("OTELC_RUST_PLAN", plan_path)
            .env("OTELC_RUST_SDK", &sdk)
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
    Ok(Command::new(binary)
        .args(application_args)
        .env("OTELC_RUST_PLAN", plan_path)
        .status()?
        .code()
        .unwrap_or(1))
}
