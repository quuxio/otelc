//! Bounded protocol shared by the stable adapter and the pinned compiler frontend.
use crate::compiler_inventory::Definition;
use anyhow::{bail, Context, Result};
use quux_otelc_rust::policy::Plan;
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    path::{Path, PathBuf},
};

#[derive(Deserialize, Serialize)]
pub struct Request {
    pub schema_version: u32,
    pub root: PathBuf,
    pub entry: PathBuf,
    pub policy: Plan,
    pub sources: BTreeMap<PathBuf, String>,
    pub declarations: Vec<Definition>,
    pub protected_source_roots: Vec<PathBuf>,
    pub discovery_argv: Vec<String>,
}

impl Request {
    pub fn load(path: &Path) -> Result<Self> {
        let mut bytes = Vec::new();
        fs::File::open(path)?
            .take(8 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() > 8 * 1024 * 1024 {
            bail!("Rust frontend request exceeds 8 MiB");
        }
        let request: Self = serde_json::from_slice(&bytes).context("read Rust frontend request")?;
        if request.schema_version != 1
            || request.policy.backend != "llvm"
            || !request.root.is_absolute()
            || !request.entry.starts_with(&request.root)
            || !request.sources.contains_key(&request.entry)
            || request.sources.is_empty()
            || request.sources.len() > 65536
            || request.declarations.len() > 65536
            || request.protected_source_roots.is_empty()
            || request.protected_source_roots.len() > 16
            || request
                .protected_source_roots
                .iter()
                .any(|path| !path.is_absolute())
            || request.sources.iter().any(|(path, hash)| {
                !path.is_absolute()
                    || hash.len() != 64
                    || !hash
                        .bytes()
                        .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            })
        {
            bail!("invalid Rust frontend request");
        }
        request.policy.validate()?;
        Ok(request)
    }

    pub fn original(
        &self,
        file: &str,
        body: ((usize, usize), (usize, usize)),
        declaration: (usize, usize),
        closure: bool,
    ) -> Result<Option<Definition>> {
        let mut candidates = self.declarations.iter().filter(|item| {
            item.file == file
                && if closure {
                    item.site.leaf == "<closure>"
                        && (
                            item.site.declaration_start_line,
                            item.site.declaration_start_column,
                        ) == declaration
                } else {
                    item.site.leaf != "<closure>"
                        && (
                            (item.site.body_start_line, item.site.body_start_column),
                            (item.site.body_end_line, item.site.body_end_column),
                        ) == body
                }
        });
        let found = candidates.next().cloned();
        if candidates.next().is_some() {
            bail!("ambiguous original Rust declaration identity");
        }
        Ok(found)
    }
}

#[derive(Serialize)]
pub struct CompiledDefinition {
    #[serde(flatten)]
    pub definition: Definition,
    pub compiler_name: String,
    pub compiler_expansion: bool,
    pub compiled_symbol: String,
    pub emitted: bool,
}

/// Columns count original UTF-8 bytes, excluding a leading encoding marker.
pub fn position(source: &str, offset: usize) -> Result<(usize, usize)> {
    if offset > source.len() || !source.is_char_boundary(offset) {
        bail!("Rust compiler span is outside its original UTF-8 input");
    }
    let before = &source.as_bytes()[..offset];
    let start = before.iter().rposition(|byte| *byte == b'\n').map_or_else(
        || {
            if source.starts_with('\u{feff}') && offset >= 3 {
                3
            } else {
                0
            }
        },
        |index| index + 1,
    );
    Ok((
        before.iter().filter(|byte| **byte == b'\n').count() + 1,
        offset - start + 1,
    ))
}
