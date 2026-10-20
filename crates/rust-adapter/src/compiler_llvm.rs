//! Compile unchanged source with a resolved original-declaration inventory.
use crate::{
    compiler::Compilation,
    compiler_inventory::{source_definitions, Definition},
    compiler_profile::{hash, HookEvidence, Toolchain},
};
use anyhow::{bail, Context, Result};
use quux_otelc_config::Selection;
use quux_otelc_rust::policy::Plan;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    process::Command,
};

struct Inventory {
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
        functions: vec![],
        sources: BTreeMap::new(),
        discovery_argv,
    };
    let selection = Selection::new(&plan.sources.include, &plan.sources.exclude, true)?;
    for path in inputs {
        let path = path.canonicalize()?;
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
        if Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
            != original_digest
        {
            bail!("Rust source changed while reading original declarations");
        }
        let identity = relative
            .with_extension("")
            .to_string_lossy()
            .replace(['/', '\\'], ".");
        let functions = source_definitions(&source, &path, &identity, plan, selected, main)?;
        // Qualification belongs to actual compiler owners, not cfg-disabled
        // syntax or restrictions imposed solely by generated-source probes.
        inventory.functions.extend(functions);
        if inventory.functions.len() > 65536 {
            bail!("Rust compiler function inventory exceeds 65536 declarations");
        }
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
    let request_path = scratch.join("frontend-request.json");
    for (path, digest) in &inventory.sources {
        if hash(path)? != *digest {
            bail!("Rust source changed while preparing the compiler inventory");
        }
    }
    let request = crate::compiler_frontend::Request {
        schema_version: 1,
        root: root.into(),
        entry,
        policy: plan.clone(),
        sources: inventory.sources,
        declarations: inventory.functions,
        protected_source_roots: profile.protected_source_roots.clone(),
        discovery_argv: inventory.discovery_argv,
    };
    let bytes = serde_json::to_vec(&request)?;
    if bytes.len() > 8 * 1024 * 1024 {
        bail!("Rust frontend request exceeds 8 MiB");
    }
    fs::write(&request_path, bytes)?;
    let binary = scratch.join("application");
    let resolved = scratch.join("resolved.json");
    let mut args: Vec<OsString> = vec![filename.as_os_str().to_owned()];
    args.extend(tools.baseline_flags().into_iter().map(OsString::from));
    args.extend([
        "--sysroot".into(),
        profile
            .rustc
            .parent()
            .and_then(Path::parent)
            .context("qualified Rust sysroot")?
            .as_os_str()
            .to_owned(),
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
    let status = Command::new(&profile.frontend)
        .args(&args)
        .env("OTELC_RUST_LLVM_INVENTORY", &inventory_path)
        .env("OTELC_RUST_FRONTEND_REQUEST", &request_path)
        .env("OTELC_RUST_LLVM_RESOLVED", &resolved)
        .status()
        .context("compile original Rust input with LLVM hooks")?;
    if !status.success() {
        bail!("Rust LLVM compilation failed");
    }
    for (path, digest) in &request.sources {
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
