//! Public original-source compiler path, with separate MIR driver provenance.
use crate::{
    compiler::Compilation,
    compiler_frontend::Request,
    compiler_profile::{hash, Toolchain},
};
use anyhow::{bail, Context, Result};
use quux_otelc_rust::policy::Plan;
use serde::{Deserialize, Serialize};
use std::{ffi::OsString, fs, path::Path, process::Command};

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct HookEvidence {
    pub profile_sha256: String,
    pub driver_sha256: String,
    pub request_sha256: String,
    pub inventory_sha256: String,
}

pub(crate) fn compile_recorded(
    plan: &Plan,
    filename: &Path,
    root: &Path,
    scratch: &Path,
    tools: &Toolchain,
) -> Result<Compilation> {
    let mut arguments = vec![filename.as_os_str().into()];
    arguments.extend(tools.baseline_flags().into_iter().map(OsString::from));
    arguments.extend(["-o".into(), scratch.join("application").into_os_string()]);
    compile_arguments(plan, filename, root, scratch, tools, arguments)
}

pub(crate) fn compile_arguments(
    plan: &Plan,
    filename: &Path,
    root: &Path,
    scratch: &Path,
    tools: &Toolchain,
    mut arguments: Vec<OsString>,
) -> Result<Compilation> {
    let profile = tools
        .mir
        .as_ref()
        .context("missing stable Rust compiler profile")?;
    let entry = filename.canonicalize()?;
    if !entry.starts_with(root) {
        bail!("Rust source is outside the project root");
    }
    // A previously compiled local Cargo dependency can already reference the
    // probe SDK. Original-input discovery must resolve that same crate graph.
    arguments.extend([
        "-L".into(),
        format!(
            "dependency={}",
            tools
                .sdk
                .parent()
                .context("Rust SDK directory")?
                .join("deps")
                .display()
        )
        .into(),
    ]);
    let (inputs, discovery) =
        crate::compiler_inputs::discover_arguments(&arguments, root, scratch, tools)?;
    let inventory = crate::compiler_llvm::inventory(
        root,
        &entry,
        plan,
        &profile.protected_source_roots,
        inputs,
        discovery,
    )?;
    let request = Request {
        schema_version: 1,
        root: root.into(),
        entry,
        policy: plan.clone(),
        sources: inventory.sources,
        declarations: inventory.functions,
        protected_source_roots: profile.protected_source_roots.clone(),
        discovery_argv: inventory.discovery_argv,
    };
    let request_path = scratch.join("compiler-request.json");
    let bytes = serde_json::to_vec(&request)?;
    if bytes.len() > 8 * 1024 * 1024 {
        bail!("Rust compiler request exceeds 8 MiB");
    }
    fs::write(&request_path, bytes)?;
    let inventory_path = scratch.join("inventory.json");
    let binary = scratch.join("application");
    if arguments
        .iter()
        .any(|argument| argument.to_string_lossy().starts_with("quux_otelc_rust="))
    {
        bail!("application dependency collides with the Rust probe crate");
    }
    arguments.extend([
        "--sysroot".into(),
        profile
            .rustc
            .parent()
            .and_then(Path::parent)
            .context("qualified Rust sysroot")?
            .as_os_str()
            .into(),
        "--extern".into(),
        format!("quux_otelc_rust={}", tools.sdk.display()).into(),
        "-L".into(),
        format!(
            "dependency={}",
            tools
                .sdk
                .parent()
                .context("Rust SDK directory")?
                .join("deps")
                .display()
        )
        .into(),
    ]);
    let status = Command::new(&profile.driver)
        .args(&arguments)
        .env("OTELC_RUST_MIR_REQUEST", &request_path)
        .env("OTELC_RUST_MIR_INVENTORY", &inventory_path)
        .status()
        .context("compile original Rust input with compiler probes")?;
    if !status.success() {
        bail!("Rust compiler instrumentation failed");
    }
    for (path, digest) in &request.sources {
        if hash(path)? != *digest {
            bail!("Rust source changed during compiler instrumentation");
        }
    }
    Ok(Compilation {
        binary,
        argv: arguments
            .iter()
            .map(|value| {
                value
                    .to_str()
                    .map(str::to_owned)
                    .context("Rust compiler arguments require UTF-8 paths")
            })
            .collect::<Result<_>>()?,
        baseline_flags: tools.baseline_flags(),
        hook: None,
        resolved: None,
        mir_hook: Some(HookEvidence {
            profile_sha256: profile.fingerprint.clone(),
            driver_sha256: profile.driver_sha256.clone(),
            request_sha256: hash(&request_path)?,
            inventory_sha256: hash(&inventory_path)?,
        }),
        request: Some(request_path),
        inventory: Some(inventory_path),
        profile: Some(profile.bytes.clone()),
    })
}
