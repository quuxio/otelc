//! Ask the matched compiler which original files this crate actually consumes.
use crate::compiler_profile::Toolchain;
use anyhow::{bail, Context, Result};
use std::{
    collections::BTreeSet,
    ffi::OsString,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    process::Command,
};

fn dependencies(text: &str, root: &Path) -> Result<Vec<PathBuf>> {
    let joined = text.replace("\\\n", "");
    let line = joined
        .lines()
        .next()
        .context("empty Rust dependency information")?;
    let (_, values) = line
        .split_once(": ")
        .context("invalid Rust dependency information")?;
    let mut paths = BTreeSet::new();
    let mut value = String::new();
    let mut characters = values.chars().peekable();
    while let Some(character) = characters.next() {
        if character == '\\' {
            value.push(
                characters
                    .next()
                    .context("unfinished Rust dependency path escape")?,
            );
        } else if character == '$' && characters.peek() == Some(&'$') {
            characters.next();
            value.push('$');
        } else if character.is_whitespace() {
            if !value.is_empty() {
                paths.insert(root.join(&value).components().collect::<PathBuf>());
                value.clear();
            }
        } else {
            value.push(character);
        }
    }
    if !value.is_empty() {
        paths.insert(root.join(value).components().collect::<PathBuf>());
    }
    if paths.is_empty() || paths.len() > 65536 {
        bail!("Rust compiler input inventory is empty or exceeds 65536 files");
    }
    Ok(paths.into_iter().collect())
}

pub(crate) fn discover(
    filename: &Path,
    root: &Path,
    scratch: &Path,
    tools: &Toolchain,
) -> Result<(Vec<PathBuf>, Vec<String>)> {
    let depfile = scratch.join("original.d");
    let mut arguments: Vec<OsString> = vec![filename.as_os_str().to_owned()];
    arguments.extend(tools.baseline_flags().into_iter().map(OsString::from));
    arguments.push(format!("--emit=dep-info={}", depfile.display()).into());
    let compiled = Command::new(&tools.rustc)
        .args(&arguments)
        .output()
        .context("discover original Rust compiler inputs")?;
    if !compiled.status.success() {
        std::io::stderr().write_all(&compiled.stderr)?;
        bail!("Rust compiler input discovery failed");
    }
    let mut bytes = Vec::new();
    fs::File::open(depfile)?
        .take(4 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 4 * 1024 * 1024 {
        bail!("Rust compiler dependency information exceeds 4 MiB");
    }
    let inputs = dependencies(std::str::from_utf8(&bytes)?, root)?;
    // Do not retain env-dep comments: env! inputs can contain application secrets.
    let argv = arguments
        .iter()
        .map(|arg| {
            arg.to_str()
                .map(str::to_owned)
                .context("Rust compiler arguments require UTF-8 paths")
        })
        .collect::<Result<_>>()?;
    Ok((inputs, argv))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dependency_paths_preserve_escapes_without_recording_environment_values() {
        let paths = dependencies("/scratch/input.d: main.rs folder\\ name/λ.rs hash\\#cash$$.rs back\\\\slash.rs \\\n main.rs\n\n# env-dep:SECRET=do-not-retain\n", Path::new("/project")).unwrap();
        assert_eq!(
            paths,
            vec![
                PathBuf::from("/project/back\\slash.rs"),
                PathBuf::from("/project/folder name/λ.rs"),
                PathBuf::from("/project/hash#cash$.rs"),
                PathBuf::from("/project/main.rs")
            ]
        );
        for bad in ["", "bad data", "/out.d: ", "/out.d: partial\\"] {
            assert!(dependencies(bad, Path::new("/project")).is_err());
        }
    }
}
