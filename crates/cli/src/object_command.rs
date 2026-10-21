//! Narrow SDK-owned native object transformation with checked provenance.
use anyhow::{bail, Context, Result};
use quux_otelc_symbols::object_transform::MainRename;
use std::{io::Write, path::Path, process::Command};

const USAGE: &str = "object rename-main INPUT.o --output NEW.o --symbol NEW_MAIN";

pub fn run(args: &[String]) -> Result<i32> {
    if args == ["--help"] || args == ["rename-main", "--help"] {
        println!("{USAGE}\nValidate and rename main in a compiled native object; write a fresh object and SDK marker.");
        return Ok(0);
    }
    if args.len() != 6 || args[0] != "rename-main" {
        bail!(USAGE);
    }
    let mut output = None;
    let mut symbol = None;
    for pair in args[2..].as_chunks::<2>().0 {
        let slot = match pair[0].as_str() {
            "--output" => &mut output,
            "--symbol" => &mut symbol,
            _ => bail!("unknown object rename-main option"),
        };
        if slot.replace(pair[1].as_str()).is_some() {
            bail!("duplicate object rename-main option");
        }
    }
    let output = Path::new(output.context("object rename-main requires --output")?);
    let symbol = symbol.context("object rename-main requires --symbol")?;
    let mut marker_name = output.as_os_str().to_os_string();
    marker_name.push(".otelc-object.json");
    let marker_path = Path::new(&marker_name);
    if output.symlink_metadata().is_ok() || marker_path.symlink_metadata().is_ok() {
        bail!("object transform output or marker already exists");
    }
    let transformation = MainRename::load(Path::new(&args[1]), symbol)?;
    let tool = crate::compiler::llvm_toolchain()?
        .bindir
        .join("llvm-objcopy");
    let tool_bytes = std::fs::read(&tool).context("read qualified llvm-objcopy")?;
    let version = Command::new(&tool).arg("--version").output()?;
    let version_text = String::from_utf8_lossy(&version.stdout);
    if !version.status.success()
        || !version_text.contains("LLVM version 22.")
        || version_text.len() > 4096
    {
        bail!("object transformation requires the qualified LLVM 22 objcopy");
    }
    let scratch = tempfile::tempdir()?;
    let source = scratch.path().join("original.o");
    let rewritten = scratch.path().join("renamed.o");
    std::fs::write(&source, transformation.bytes())?;
    let result = Command::new(&tool)
        .arg("--redefine-sym")
        .arg(transformation.redefine_argument())
        .arg(&source)
        .arg(&rewritten)
        .output()?;
    if !result.status.success() {
        bail!(
            "LLVM object rename failed: {}",
            String::from_utf8_lossy(&result.stderr)
        );
    }
    let bytes = std::fs::read(&rewritten)?;
    let marker = transformation.marker(&bytes, &tool_bytes, &version_text)?;
    if std::fs::read(&tool)? != tool_bytes {
        bail!("object tool changed during transformation");
    }
    publish(output, marker_path, &bytes, &marker)?;
    Ok(0)
}

fn publish(output: &Path, marker_path: &Path, bytes: &[u8], marker: &[u8]) -> Result<()> {
    let parent = output
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let mut staged_object = tempfile::NamedTempFile::new_in(parent)?;
    staged_object.write_all(bytes)?;
    let mut staged_marker = tempfile::NamedTempFile::new_in(parent)?;
    staged_marker.write_all(marker)?;
    let owned = staged_object
        .persist_noclobber(output)
        .map_err(|e| e.error)?;
    if let Err(error) = staged_marker.persist_noclobber(marker_path) {
        // Only remove the exact object published by this operation on a marker race.
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if let (Ok(actual), Ok(expected)) = (output.symlink_metadata(), owned.metadata()) {
                if actual.dev() == expected.dev() && actual.ino() == expected.ino() {
                    let _ = std::fs::remove_file(output);
                }
            }
        }
        return Err(error.error.into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn marker_publication_race_reaps_only_its_owned_object() {
        let root = tempfile::tempdir().unwrap();
        let output = root.path().join("new.o");
        let marker = root.path().join("new.o.otelc-object.json");
        std::fs::write(&marker, "other owner").unwrap();
        assert!(publish(&output, &marker, b"object", b"marker").is_err());
        assert!(!output.exists());
        assert_eq!(std::fs::read(&marker).unwrap(), b"other owner");
        std::fs::write(&output, "old object").unwrap();
        assert!(publish(&output, &marker, b"new object", b"marker").is_err());
        assert_eq!(std::fs::read(&output).unwrap(), b"old object");
    }
    #[test]
    fn incomplete_unknown_and_duplicate_operations_are_rejected() {
        for args in [
            vec![],
            vec!["unknown"],
            vec![
                "rename-main",
                "input",
                "--output",
                "out",
                "--output",
                "other",
            ],
            vec![
                "rename-main",
                "input",
                "--unknown",
                "out",
                "--symbol",
                "new_main",
            ],
        ] {
            assert!(run(&args.into_iter().map(str::to_owned).collect::<Vec<_>>()).is_err());
        }
    }
}
