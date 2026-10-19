//! Resolve compiler-hook identities from original declarations and common policy.
use anyhow::{bail, Result};
use quux_otelc_rust::policy::Plan;
use serde::Serialize;
use std::path::Path;

#[derive(Debug, Serialize)]
pub struct Definition {
    file: String,
    name: String,
    suffix: String,
    unsupported: String,
    selected: bool,
    #[serde(flatten)]
    site: crate::transform::CompilerSite,
}

/// The caller supplies the canonical original source path, never a generated copy.
pub fn source_definitions(
    source: &str,
    file: &Path,
    identity: &str,
    plan: &Plan,
    selected_source: bool,
    startup: bool,
) -> Result<Vec<Definition>> {
    if !file.is_absolute() || identity.is_empty() {
        bail!("Rust compiler inventory requires an absolute original path and identity");
    }
    let (_, functions) =
        crate::transform::transform(source, identity, plan, selected_source, startup, true)?;
    functions
        .into_iter()
        .map(|function| {
            let suffix = function
                .name
                .strip_prefix(identity)
                .and_then(|value| value.strip_prefix('.'))
                .ok_or_else(|| {
                    anyhow::anyhow!("Rust compiler definition identity is inconsistent")
                })?
                .to_owned();
            let unsupported = if function.compiler_site.asynchronous {
                "async compiler bodies require future-completion qualification"
            } else {
                function.unsupported.unwrap_or("")
            }
            .to_owned();
            Ok(Definition {
                file: file
                    .to_str()
                    .ok_or_else(|| anyhow::anyhow!("Rust compiler source path must be UTF-8"))?
                    .into(),
                name: function.name,
                suffix,
                unsupported,
                selected: function.selected,
                site: function.compiler_site,
            })
        })
        .collect()
}
