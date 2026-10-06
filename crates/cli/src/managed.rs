//! Launch language adapters with the authoritative resolved policy.
use anyhow::{bail, Context, Result};
use quux_otelc_config::common::{CommonConfig, Language};
use std::{io::Write, path::Path, process::Command};

pub fn run(
    command: &str,
    args: &[String],
    config: &Path,
    language: Option<Language>,
) -> Result<i32> {
    if command != "python" {
        bail!("unknown language adapter command");
    }
    if language.is_some_and(|l| l != Language::Python) {
        bail!("python requires --language python");
    }
    let resolved = CommonConfig::load(config, true)?.resolve(Language::Python)?;
    if !resolved.execution_available {
        bail!("{}", resolved.unavailable.join("; "));
    }
    let mut plan = tempfile::NamedTempFile::new()?;
    serde_json::to_writer(&mut plan, &resolved)?;
    plan.flush()?;
    let root = std::env::var_os("OTELC_ADAPTER_ROOT")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("../../adapters"));
    let launcher = root.join("python/launch.py");
    if !launcher.is_file() {
        bail!("Python adapter not found; set OTELC_ADAPTER_ROOT");
    }
    let local_python = root
        .parent()
        .context("adapter root")?
        .join(".venv/bin/python");
    let python = std::env::var_os("OTELC_PYTHON").unwrap_or_else(|| {
        if local_python.is_file() {
            local_python.into_os_string()
        } else {
            "python3".into()
        }
    });
    let status = Command::new(python)
        .arg(launcher)
        .arg(plan.path())
        .args(args)
        .status()
        .context("launch Python instrumentation")?;
    Ok(status.code().unwrap_or(1))
}
