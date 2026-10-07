//! Opt-in owner-only control. Requests change admission, never outstanding tokens.
use crate::*;
use quux_otelc_config::control::read_command;
use std::{
    io::Write,
    os::unix::{
        fs::{MetadataExt, PermissionsExt},
        net::UnixListener,
    },
    path::Path,
};
pub(super) struct Thread {
    pub handle: std::thread::JoinHandle<()>,
    pub done: mpsc::Receiver<()>,
}
pub(super) struct Bound {
    listener: UnixListener,
    path: PathBuf,
    inode: u64,
}
impl Drop for Bound {
    fn drop(&mut self) {
        if std::fs::symlink_metadata(&self.path).is_ok_and(|m| m.ino() == self.inode) {
            let _ = std::fs::remove_file(&self.path);
        }
    }
}
pub(super) fn bind(value: &str) -> Result<Bound> {
    let path = Path::new(value);
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .context("control socket requires a private parent directory")?;
    let metadata =
        std::fs::symlink_metadata(parent).context("create a private control directory first")?;
    if !metadata.is_dir()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.mode() & 0o077 != 0
    {
        anyhow::bail!("control directory must be owned by this user with mode 0700");
    }
    // Binding fails on an occupied path. Never unlink another process's socket.
    let listener = UnixListener::bind(path).context("bind control socket (path must be unused)")?;
    let bound = Bound {
        listener,
        path: path.into(),
        inode: std::fs::symlink_metadata(path)?.ino(),
    };
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    bound.listener.set_nonblocking(true)?;
    Ok(bound)
}
fn apply(command: &str, enabled: &AtomicBool) -> Result<()> {
    match command {
        "status" => (),
        "enable" => enabled.store(true, Ordering::Release),
        "disable" => enabled.store(false, Ordering::Release),
        _ => anyhow::bail!("expected status, enable or disable"),
    }
    Ok(())
}
pub(super) fn serve(bound: Bound, state: &'static State) {
    while state.running.load(Ordering::Acquire) {
        match bound.listener.accept() {
            Ok((mut stream, _)) => {
                let result = read_command(&mut stream)
                    .and_then(|command| apply(&command, &state.metrics_enabled));
                let response = match result {
                    Ok(()) => {
                        serde_json::json!({"schema_version":1,"pid":std::process::id(),"metrics_enabled":state.metrics_enabled.load(Ordering::Acquire),"function_calls":state.completed.load(Ordering::Acquire)})
                    }
                    Err(_) => serde_json::json!({"error":"invalid or incomplete control request"}),
                };
                let _ = stream.set_write_timeout(Some(Duration::from_millis(200)));
                if let Ok(mut bytes) = serde_json::to_vec(&response) {
                    bytes.push(b'\n');
                    let _ = stream.write_all(&bytes);
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(2))
            }
            Err(_) => break,
        }
    }
}
#[cfg(test)]
#[path = "tests/control.rs"]
mod tests;
