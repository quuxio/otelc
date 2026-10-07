//! Small client for the runtime's versioned owner-only admission control.
use anyhow::{bail, Context, Result};
use std::{
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{FileTypeExt, PermissionsExt},
        net::UnixStream,
    },
    time::Duration,
};
pub fn run(command: &str, args: &[String]) -> Result<i32> {
    if args.len() != 2 || args[0] != "--socket" {
        bail!("{command} requires --socket PATH");
    }
    let metadata = std::fs::symlink_metadata(&args[1]).context("find metrics control socket")?;
    if !metadata.file_type().is_socket() || metadata.permissions().mode() & 0o077 != 0 {
        bail!("control socket must be a private Unix socket");
    }
    let mut stream = UnixStream::connect(&args[1]).context("connect metrics control socket")?;
    stream.set_read_timeout(Some(Duration::from_secs(1)))?;
    stream.set_write_timeout(Some(Duration::from_secs(1)))?;
    writeln!(stream, "{command}")?;
    let mut response = String::new();
    BufReader::new(stream).take(4096).read_line(&mut response)?;
    let value: serde_json::Value =
        serde_json::from_str(&response).context("invalid metrics control response")?;
    if value.get("error").is_some()
        || value["schema_version"] != 1
        || !value["metrics_enabled"].is_boolean()
        || !value["pid"].is_u64()
        || !value["function_calls"].is_u64()
    {
        bail!("runtime rejected metrics control request");
    }
    println!("{}", serde_json::to_string_pretty(&value)?);
    Ok(0)
}
