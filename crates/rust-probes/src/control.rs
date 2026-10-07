use crate::Runtime;
use anyhow::{bail, Result};
use std::{
    fs,
    io::{Read, Write},
    os::unix::{
        fs::{MetadataExt, PermissionsExt},
        net::{UnixListener, UnixStream},
    },
    path::{Path, PathBuf},
    sync::{atomic::Ordering, Arc},
    time::{Duration, Instant},
};
pub(crate) struct Control {
    path: PathBuf,
    device: u64,
    inode: u64,
}
impl Control {
    pub fn bind(path: &str, runtime: Arc<Runtime>) -> Result<Self> {
        let path = PathBuf::from(path);
        let parent = fs::symlink_metadata(path.parent().unwrap_or(Path::new(".")))?;
        // SAFETY: geteuid has no pointer arguments and cannot alter process state.
        if !parent.is_dir()
            || parent.permissions().mode() & 0o777 != 0o700
            || parent.uid() != unsafe { libc::geteuid() }
        {
            bail!("Rust control parent must be owner-only");
        }
        if fs::symlink_metadata(&path).is_ok() {
            bail!("Rust control path already exists");
        }
        let listener = UnixListener::bind(&path)?;
        let info = fs::symlink_metadata(&path)?;
        let control = Self {
            path,
            device: info.dev(),
            inode: info.ino(),
        };
        fs::set_permissions(&control.path, fs::Permissions::from_mode(0o600))?;
        listener.set_nonblocking(true)?;
        std::thread::Builder::new()
            .name("otelc-rust-control".into())
            .spawn(move || {
                while !runtime.state.closed.load(Ordering::Acquire) {
                    match listener.accept() {
                        Ok((mut stream, _)) => {
                            let _ = respond(&mut stream, &runtime);
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            std::thread::sleep(Duration::from_millis(5))
                        }
                        Err(_) => break,
                    }
                }
            })?;
        Ok(control)
    }
    pub fn close(self) {
        drop(self);
    }
}
impl Drop for Control {
    fn drop(&mut self) {
        if let Ok(info) = fs::symlink_metadata(&self.path) {
            if info.dev() == self.device && info.ino() == self.inode {
                let _ = fs::remove_file(&self.path);
            }
        }
    }
}
fn respond(stream: &mut UnixStream, runtime: &Runtime) -> Result<()> {
    // macOS accepted sockets can inherit the listener's nonblocking flag.
    stream.set_nonblocking(false)?;
    let end = Instant::now() + Duration::from_millis(200);
    let mut request = Vec::new();
    let mut byte = [0];
    while request.len() < 18 {
        let remaining = end.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        stream.set_read_timeout(Some(remaining))?;
        if stream.read(&mut byte).ok() != Some(1) {
            break;
        }
        request.push(byte[0]);
        if byte[0] == b'\n' {
            break;
        }
    }
    let invalid = match request.as_slice() {
        b"enable\n" => {
            runtime.state.enabled.store(true, Ordering::Release);
            false
        }
        b"disable\n" => {
            runtime.state.enabled.store(false, Ordering::Release);
            false
        }
        b"status\n" => false,
        _ => true,
    };
    let value = if invalid {
        serde_json::json!({"error":"invalid control request"})
    } else {
        serde_json::json!({"schema_version":1,"pid":std::process::id(),"metrics_enabled":runtime.state.enabled.load(Ordering::Acquire),"function_calls":runtime.report()["function_calls"]})
    };
    let mut data = serde_json::to_vec(&value)?;
    data.push(b'\n');
    stream.set_write_timeout(Some(Duration::from_millis(200)))?;
    stream.write_all(&data)?;
    Ok(())
}
