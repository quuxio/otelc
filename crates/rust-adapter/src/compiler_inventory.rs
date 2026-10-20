//! Resolve compiler-hook identities from original declarations and common policy.
use anyhow::{bail, Result};
use quux_otelc_rust::policy::Plan;
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Definition {
    pub file: String,
    pub name: String,
    pub suffix: String,
    pub unsupported: String,
    pub selected: bool,
    #[serde(flatten)]
    pub site: crate::transform::CompilerSite,
}

/// The caller supplies the absolute original compiler source path, never a generated copy.
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
        crate::transform::compiler_definitions(source, identity, plan, selected_source, startup)?;
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
