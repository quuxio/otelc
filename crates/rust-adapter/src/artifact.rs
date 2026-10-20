//! Explicit build artefacts separate compiler channels from application execution.
use crate::compiler_profile::{HookEvidence, Toolchain};
use anyhow::{bail, Context, Result};
use quux_otelc_rust::policy::Plan;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Compiler {
    program: String,
    argv: Vec<String>,
    version: String,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Build {
    schema_version: u32,
    language: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    backend: Option<String>,
    executable_sha256: String,
    plan_sha256: String,
    adapter_sha256: String,
    sdk_sha256: String,
    source: PathBuf,
    source_sha256: String,
    source_root: PathBuf,
    baseline_flags: Vec<String>,
    compiler: Compiler,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    compiler_hook: Option<HookEvidence>,
}

fn hash(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut bytes = [0; 65536];
    loop {
        let count = file.read(&mut bytes)?;
        if count == 0 {
            break;
        }
        digest.update(&bytes[..count]);
    }
    Ok(digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>())
}

fn read(path: &Path) -> Result<Vec<u8>> {
    if !fs::symlink_metadata(path)?.is_file() {
        bail!("Rust artifact inputs must be regular files");
    }
    let mut bytes = Vec::new();
    File::open(path)?
        .take(4 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 4 * 1024 * 1024 {
        bail!("Rust artifact metadata exceeds size bound");
    }
    Ok(bytes)
}

fn write(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(bytes)?;
    Ok(())
}

struct PendingDirectory {
    path: PathBuf,
    complete: bool,
}
impl Drop for PendingDirectory {
    fn drop(&mut self) {
        if !self.complete {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

pub(crate) fn build(
    plan_path: &Path,
    source: &Path,
    directory: &Path,
    root: &Path,
    tools: &Toolchain,
    adapter: &Path,
) -> Result<()> {
    if directory.try_exists()? {
        bail!("Rust artifact output directory already exists");
    }
    let source_path = source.canonicalize()?;
    let source_sha256 = hash(&source_path)?;
    let plan_bytes = read(plan_path)?;
    let plan: Plan = serde_json::from_slice(&plan_bytes)?;
    plan.validate()?;
    let scratch = tempfile::tempdir()?;
    let compiled = crate::compiler::compile_recorded(&plan, source, root, scratch.path(), tools)?;
    if hash(&source_path)? != source_sha256 {
        bail!("Rust source changed during the build");
    }
    let version = Command::new(&tools.rustc)
        .arg(if tools.llvm.is_some() {
            "-vV"
        } else {
            "--version"
        })
        .output()?;
    if !version.status.success() {
        bail!("cannot retain Rust compiler version");
    }
    let mut metadata = Build {
        schema_version: if tools.llvm.is_some() { 2 } else { 1 },
        language: "rust".into(),
        backend: tools.llvm.as_ref().map(|_| "llvm".into()),
        executable_sha256: hash(&compiled.binary)?,
        plan_sha256: String::new(),
        adapter_sha256: hash(adapter)?,
        sdk_sha256: hash(&tools.sdk)?,
        source: source_path,
        source_sha256,
        source_root: root.to_owned(),
        baseline_flags: compiled.baseline_flags,
        compiler: Compiler {
            program: tools.rustc.clone(),
            argv: compiled.argv,
            version: String::from_utf8(version.stdout)?.trim().to_owned(),
        },
        compiler_hook: compiled.hook,
    };
    // Exclusive creation refuses collisions. The manifest is the last file, so
    // interrupted/incomplete bundles fail validation rather than launching.
    fs::DirBuilder::new().mode(0o700).create(directory)?;
    let mut pending = PendingDirectory {
        path: directory.to_owned(),
        complete: false,
    };
    fs::copy(compiled.binary, directory.join("application"))?;
    write(&directory.join("plan.json"), &plan_bytes)?;
    if let Some(inventory) = compiled.inventory {
        write(&directory.join("inventory.json"), &read(&inventory)?)?;
    }
    if let Some(profile) = compiled.profile {
        write(&directory.join("profile.json"), &profile)?;
    }
    if let Some(resolved) = compiled.resolved {
        write(&directory.join("resolved.json"), &read(&resolved)?)?;
    }
    metadata.plan_sha256 = hash(&directory.join("plan.json"))?;
    write(
        &directory.join("build.json"),
        &serde_json::to_vec_pretty(&metadata)?,
    )?;
    pending.complete = true;
    Ok(())
}

pub(crate) fn launch(
    plan_path: &Path,
    directory: &Path,
    args: &[String],
    adapter: &Path,
) -> Result<i32> {
    let directory = directory
        .canonicalize()
        .context("locate Rust artifact bundle")?;
    let metadata: Build = serde_json::from_slice(&read(&directory.join("build.json"))?)?;
    if !matches!(metadata.schema_version, 1 | 2) || metadata.language != "rust" {
        bail!("unsupported Rust artifact format");
    }
    if let Some(hook) = &metadata.compiler_hook {
        let inventory = read(&directory.join("inventory.json"))?;
        if Sha256::digest(&inventory)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
            != hook.inventory_sha256
        {
            bail!("Rust compiler inventory fingerprint mismatch");
        }
        if Sha256::digest(read(&directory.join("resolved.json"))?)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
            != hook.resolved_sha256
        {
            bail!("Rust resolved compiler provenance fingerprint mismatch");
        }
        if Sha256::digest(read(&directory.join("profile.json"))?)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
            != hook.profile_sha256
        {
            bail!("Rust compiler profile fingerprint mismatch");
        }
    }
    let binary = directory.join("application");
    if !fs::symlink_metadata(&binary)?.is_file() || hash(&binary)? != metadata.executable_sha256 {
        bail!("Rust artifact executable fingerprint mismatch");
    }
    if hash(adapter)? != metadata.adapter_sha256 {
        bail!("Rust artifact requires its matching adapter; use that adapter or rebuild");
    }
    let stored_path = directory.join("plan.json");
    let stored_bytes = read(&stored_path)?;
    if Sha256::digest(&stored_bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>()
        != metadata.plan_sha256
    {
        bail!("Rust artifact policy fingerprint mismatch");
    }
    let stored: serde_json::Value = serde_json::from_slice(&stored_bytes)?;
    let llvm = stored["backend"] == "llvm";
    if llvm != metadata.compiler_hook.is_some()
        || llvm != (metadata.backend.as_deref() == Some("llvm"))
        || (llvm && metadata.schema_version != 2)
        || (!llvm && (metadata.schema_version != 1 || metadata.backend.is_some()))
    {
        bail!("Rust compiler artifact profile evidence is incomplete");
    }
    let current: serde_json::Value = serde_json::from_slice(&read(plan_path)?)?;
    for key in [
        "schema_version",
        "language",
        "backend",
        "sources",
        "functions",
        "source_matchers",
        "function_matchers",
        "annotations",
    ] {
        if stored.get(key) != current.get(key) {
            bail!("Rust artifact selection or annotation policy changed; rebuild the artifact");
        }
    }
    if let Some(target) = std::env::var_os("OTELC_COVERAGE_BIN_DIR") {
        fs::create_dir_all(&target)?;
        fs::copy(
            &binary,
            PathBuf::from(target).join(format!("rust-artifact-{}", std::process::id())),
        )?;
    }
    Ok(quux_otelc_process::child_code(
        Command::new(binary)
            .args(args)
            .env("OTELC_RUST_PLAN", plan_path)
            .status()?,
    )?)
}
