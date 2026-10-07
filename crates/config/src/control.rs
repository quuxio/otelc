//! Unix control framing shared by the native and Rust runtime backends.
use anyhow::{Context, Result};
use std::{
    io::Read,
    os::{fd::AsRawFd, unix::net::UnixStream},
    time::{Duration, Instant},
};

pub fn read_command(stream: &mut UnixStream) -> Result<String> {
    // Accepted sockets can inherit nonblocking mode on macOS. Wait explicitly
    // for readiness under one deadline, without resetting SO_RCVTIMEO per byte.
    stream.set_nonblocking(true)?;
    let deadline = Instant::now() + Duration::from_millis(200);
    let mut bytes = Vec::new();
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            anyhow::bail!("control request timed out");
        }
        let timeout = remaining
            .as_millis()
            .saturating_add(1)
            .min(i32::MAX as u128) as i32;
        let mut descriptor = libc::pollfd {
            fd: stream.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        // SAFETY: descriptor references one live fd borrowed for this call.
        let ready = unsafe { libc::poll(&mut descriptor, 1, timeout) };
        if ready < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error.into());
        }
        if ready == 0 || Instant::now() >= deadline {
            anyhow::bail!("control request timed out");
        }
        let mut byte = [0];
        match stream.read_exact(&mut byte) {
            Ok(()) => (),
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                ) =>
            {
                continue
            }
            Err(error) => return Err(error.into()),
        }
        if byte[0] == b'\n' {
            break;
        }
        bytes.push(byte[0]);
        if bytes.len() > 16 {
            anyhow::bail!("control request too long");
        }
    }
    String::from_utf8(bytes).context("control request must be UTF-8")
}
