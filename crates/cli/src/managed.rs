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
    let target = match command {
        "python" => Language::Python,
        "node" => Language::JavaScript,
        "ts" => Language::TypeScript,
        "java" => Language::Java,
        "go" => Language::Go,
        "rust" => Language::Rust,
        _ => bail!("unknown language adapter command"),
    };
    if language.is_some_and(|l| l != target) {
        bail!("{command} requires --language {target}");
    }
    let resolved = CommonConfig::load(config, true)?.resolve(target)?;
    if !resolved.execution_available {
        bail!("{}", resolved.unavailable.join("; "));
    }
    let mut plan = tempfile::NamedTempFile::new()?;
    serde_json::to_writer(&mut plan, &resolved)?;
    plan.flush()?;
    if target == Language::Rust {
        let adapter = std::env::current_exe()?
            .parent()
            .context("CLI directory")?
            .join("otelc-rust-adapter");
        if !adapter.is_file() {
            bail!("Rust adapter not found; build with make build");
        }
        let status = Command::new(adapter)
            .arg(plan.path())
            .args(args)
            .status()
            .context("launch Rust instrumentation")?;
        return Ok(status.code().unwrap_or(1));
    }
    let root = std::env::var_os("OTELC_ADAPTER_ROOT")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("../../adapters"));
    if target == Language::Go {
        let adapter = root.join("go/build/otelc-go");
        if !adapter.is_file() {
            bail!("Go adapter not found; build with make go-build or set OTELC_ADAPTER_ROOT");
        }
        let status = Command::new(adapter)
            .arg(plan.path())
            .args(args)
            .status()
            .context("launch Go instrumentation")?;
        return Ok(status.code().unwrap_or(1));
    }
    if target == Language::Java {
        let agent = root.join("java/target/java-agent-0.1.0-agent.jar");
        if !agent.is_file() {
            bail!("Java agent not found; build with make java-check or set OTELC_ADAPTER_ROOT");
        }
        if args.is_empty() {
            bail!("java requires CLASS, SOURCE.java or -jar JAR [ARGS...]");
        }
        let java = std::env::var_os("OTELC_JAVA").unwrap_or_else(|| "java".into());
        let mut child = Command::new(java);
        if args[0] == "--doctor" || args[0] == "--inspect" {
            child.arg("-jar").arg(&agent).arg(plan.path());
        } else {
            if resolved.propagation.tasks {
                child.arg("-Xshare:off");
            }
            child.arg(format!(
                "-javaagent:{}={}",
                agent.display(),
                plan.path().display()
            ));
        }
        let status = child
            .args(args)
            .status()
            .context("launch Java instrumentation")?;
        return Ok(status.code().unwrap_or(1));
    }
    if matches!(target, Language::JavaScript | Language::TypeScript) {
        let launcher = root.join("node/register.mjs");
        if !launcher.is_file() {
            bail!("Node adapter not found; set OTELC_ADAPTER_ROOT");
        }
        if args.is_empty() {
            bail!("node requires SCRIPT [ARGS...]");
        }
        let node = std::env::var_os("OTELC_NODE").unwrap_or_else(|| "node".into());
        let mut child = Command::new(node);
        if args[0] == "--doctor" || args[0] == "--inspect" {
            child.arg(root.join("node/cli.mjs")).arg(plan.path());
        } else {
            if args[0].starts_with('-') {
                bail!("node requires a script path; Node flags are not accepted");
            }
            child
                .arg("--enable-source-maps")
                .arg("--import")
                .arg(launcher);
            child.env("OTELC_NODE_PLAN", plan.path());
        }
        let status = child
            .args(args)
            .status()
            .context("launch Node instrumentation")?;
        return Ok(status.code().unwrap_or(1));
    }
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
