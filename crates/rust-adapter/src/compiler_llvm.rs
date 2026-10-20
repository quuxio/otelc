//! Compile unchanged source with a resolved original-declaration inventory.
use crate::{
    compiler::Compilation,
    compiler_inventory::{source_definitions, Definition},
    compiler_profile::{hash, HookEvidence, Toolchain},
};
use anyhow::{bail, Context, Result};
use quux_otelc_config::Selection;
use quux_otelc_rust::policy::Plan;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Serialize)]
struct Inventory {
    schema_version: u32,
    functions: Vec<Definition>,
    sources: BTreeMap<PathBuf, String>,
    discovery_argv: Vec<String>,
}

fn inventory(
    root: &Path,
    entry: &Path,
    plan: &Plan,
    protected: &[PathBuf],
    inputs: Vec<PathBuf>,
    discovery_argv: Vec<String>,
) -> Result<Inventory> {
    let mut inventory = Inventory {
        schema_version: 2,
        functions: vec![],
        sources: BTreeMap::new(),
        discovery_argv,
    };
    let selection = Selection::new(&plan.sources.include, &plan.sources.exclude, true)?;
    let mut startup = false;
    for path in inputs {
        let original_digest = hash(&path)?;
        inventory
            .sources
            .insert(path.clone(), original_digest.clone());
        let canonical = path.canonicalize()?;
        if protected
            .iter()
            .any(|excluded| canonical.starts_with(excluded))
        {
            continue;
        }
        let Ok(relative) = path.strip_prefix(root) else {
            continue;
        };
        if path.extension().is_none_or(|extension| extension != "rs") {
            continue;
        }
        let selected = selection.accepts(&relative.to_string_lossy().replace('\\', "/"));
        let main = canonical == entry;
        if !selected && !main {
            continue;
        }
        let source = fs::read_to_string(&path)?;
        if format!("{:x}", Sha256::digest(source.as_bytes())) != original_digest {
            bail!("Rust source changed while reading original declarations");
        }
        let identity = relative
            .with_extension("")
            .to_string_lossy()
            .replace(['/', '\\'], ".");
        let functions = source_definitions(&source, &path, &identity, plan, selected, main)?;
        for function in &functions {
            if function.selected_or_startup() {
                if let Some(reason) = function.unsupported_reason() {
                    bail!("Rust LLVM: {reason}");
                }
            }
            startup |= function.startup();
        }
        inventory.functions.extend(functions);
        if inventory.functions.len() > 65536 {
            bail!("Rust compiler function inventory exceeds 65536 declarations");
        }
    }
    if !startup {
        bail!("Rust LLVM executable requires an ordinary source-defined main");
    }
    Ok(inventory)
}

pub(crate) fn compile_recorded(
    plan: &Plan,
    filename: &Path,
    root: &Path,
    scratch: &Path,
    tools: &Toolchain,
) -> Result<Compilation> {
    let profile = tools
        .llvm
        .as_ref()
        .context("missing Rust LLVM compiler profile")?;
    let entry = filename.canonicalize()?;
    if !entry.starts_with(root) {
        bail!("Rust source is outside the project root");
    }
    let (inputs, discovery) = crate::compiler_inputs::discover(filename, root, scratch, tools)?;
    let inventory = inventory(
        root,
        &entry,
        plan,
        &profile.protected_source_roots,
        inputs,
        discovery,
    )?;
    let inventory_path = scratch.join("inventory.json");
    let bytes = serde_json::to_vec(&inventory)?;
    if bytes.len() > 4 * 1024 * 1024 {
        bail!("Rust compiler inventory exceeds 4 MiB");
    }
    fs::write(&inventory_path, bytes)?;
    for (path, digest) in &inventory.sources {
        if hash(path)? != *digest {
            bail!("Rust source changed while preparing the compiler inventory");
        }
    }
    let binary = scratch.join("application");
    let resolved = scratch.join("resolved.json");
    let mut args: Vec<OsString> = vec![filename.as_os_str().to_owned()];
    args.extend(tools.baseline_flags().into_iter().map(OsString::from));
    args.extend([
        "-o".into(),
        binary.as_os_str().to_owned(),
        format!("-Zllvm-plugins={}", profile.plugin.display()).into(),
        "-Zunstable-options".into(),
        "--extern".into(),
        format!(
            "noprelude,force,nounused:quux_otelc_rust={}",
            tools.sdk.display()
        )
        .into(),
        "-L".into(),
        format!(
            "dependency={}",
            tools
                .sdk
                .parent()
                .context("Rust LLVM SDK directory")?
                .join("deps")
                .display()
        )
        .into(),
    ]);
    let status = Command::new(&tools.rustc)
        .args(&args)
        .env("OTELC_RUST_LLVM_INVENTORY", &inventory_path)
        .env("OTELC_RUST_LLVM_RESOLVED", &resolved)
        .status()
        .context("compile original Rust input with LLVM hooks")?;
    if !status.success() {
        bail!("Rust LLVM compilation failed");
    }
    for (path, digest) in &inventory.sources {
        if hash(path)? != *digest {
            bail!("Rust source changed during LLVM compilation");
        }
    }
    let hook = HookEvidence {
        profile_sha256: profile.fingerprint.clone(),
        plugin_sha256: profile.plugin_sha256.clone(),
        inventory_sha256: hash(&inventory_path)?,
        resolved_sha256: hash(&resolved)?,
    };
    Ok(Compilation {
        binary,
        argv: args
            .iter()
            .map(|arg| {
                arg.to_str()
                    .map(str::to_owned)
                    .context("Rust build arguments require UTF-8 paths")
            })
            .collect::<Result<_>>()?,
        baseline_flags: tools.baseline_flags(),
        hook: Some(hook),
        inventory: Some(inventory_path),
        profile: Some(profile.bytes.clone()),
        resolved: Some(resolved),
    })
}
