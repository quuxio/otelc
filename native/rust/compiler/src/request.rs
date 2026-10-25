//! Closed compiler component request; common policy resolution stays in tooling.
use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::Read,
    path::{Path, PathBuf},
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    pub schema_version: u32,
    pub mode: String,
    pub root: PathBuf,
    pub entry: PathBuf,
    pub output: PathBuf,
    pub sources: BTreeMap<PathBuf, String>,
    pub selected: BTreeSet<String>,
    pub lifetimes: Option<BTreeMap<String, BTreeMap<u32, String>>>,
    pub lifetime_bodies: Option<BTreeMap<String, String>>,
}

#[derive(Clone, Serialize)]
pub(crate) struct Definition {
    pub identity: String,
    pub file: PathBuf,
    pub compiler_name: String,
    pub start: u32,
    pub end: u32,
    pub asynchronous: bool,
    pub expanded: bool,
}

pub(crate) fn digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

impl Request {
    pub fn weaving(&self) -> bool {
        matches!(self.mode.as_str(), "weave" | "lifetime-weave")
    }

    fn profile(&self) -> bool {
        if self.schema_version == 1 {
            return matches!(self.mode.as_str(), "inspect" | "weave" | "lifetime-inspect")
                && self.lifetimes.is_none()
                && self.lifetime_bodies.is_none();
        }
        let Some(bodies) = &self.lifetimes else {
            return false;
        };
        let Some(fingerprints) = &self.lifetime_bodies else {
            return false;
        };
        self.schema_version == 2
            && self.mode == "lifetime-weave"
            && !bodies.is_empty()
            && bodies.len() <= 4096
            && bodies.values().map(BTreeMap::len).sum::<usize>() <= 65536
            && bodies.keys().eq(fingerprints.keys())
            && fingerprints.values().all(|value| {
                value.len() == 64
                    && value
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
            && bodies.iter().all(|(name, locals)| {
                !name.is_empty()
                    && name.len() <= 4096
                    && !name.contains('\0')
                    && !locals.is_empty()
                    && locals.len() <= 4096
                    && locals.iter().all(|(local, name)| {
                        *local > 0
                            && *local < 65536
                            && !name.is_empty()
                            && name.len() <= 4096
                            && !name.contains('\0')
                    })
            })
    }

    pub fn load(path: &Path) -> Result<Self> {
        let mut bytes = Vec::new();
        fs::File::open(path)?
            .take(8 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() > 8 * 1024 * 1024 {
            bail!("Rust compiler request exceeds 8 MiB");
        }
        let value: Self = serde_json::from_slice(&bytes).context("read Rust compiler request")?;
        if !value.profile()
            || value.root.canonicalize()? != value.root
            || !value.root.is_dir()
            || value.entry.canonicalize()? != value.entry
            || !value.entry.starts_with(&value.root)
            || !value.sources.contains_key(&value.entry)
            || value.sources.len() > 65536
            || value.selected.len() > 4096
            || !value.output.is_absolute()
            || value.output.exists()
            || value.output.starts_with(&value.root)
            || value
                .output
                .parent()
                .context("missing Rust output directory")?
                .canonicalize()?
                != value
                    .output
                    .parent()
                    .context("missing Rust output directory")?
            || (!value.weaving() && !value.selected.is_empty())
        {
            bail!("invalid Rust compiler component request");
        }
        for (source, hash) in &value.sources {
            if source.canonicalize()? != *source
                || !source.starts_with(&value.root)
                || hash.len() != 64
                || !hash
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                || digest(&fs::read(source)?) != *hash
            {
                bail!("Rust original source binding differs: {}", source.display());
            }
        }
        Ok(value)
    }
}

#[cfg(test)]
#[path = "request_tests.rs"]
mod tests;
