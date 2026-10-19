//! Preserve child termination after launcher-owned resources have been dropped.
use std::{fmt, process::ExitStatus};

/// A signal is carried through normal error unwinding before the entry point exits.
#[derive(Debug)]
pub struct ChildSignal(i32);

impl fmt::Display for ChildSignal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "child terminated by signal {}", self.0)
    }
}
impl std::error::Error for ChildSignal {}

/// Return ordinary exit codes without collapsing a Unix signal into code one.
pub fn child_code(status: ExitStatus) -> Result<i32, ChildSignal> {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(signal) = status.signal() {
            return Err(ChildSignal(signal));
        }
    }
    Ok(status.code().unwrap_or(1))
}

impl ChildSignal {
    /// Re-emit the actual child signal once normal Rust cleanup has completed.
    pub fn terminate(&self) -> ! {
        #[cfg(unix)]
        // SAFETY: self contains an actual child wait-status signal; the caller
        // reaches this entry-point path after launcher-owned resources are dropped.
        unsafe {
            default_unblocked(self.0);
            // SAFETY: the signal came from an actual child wait status.
            libc::raise(self.0);
        }
        std::process::exit(128 + self.0)
    }
}

#[cfg(unix)]
fn default_unblocked(signal: i32) {
    // SAFETY: only called in the launcher entry point or an isolated test child.
    // No application handler should intercept launcher termination.
    unsafe {
        libc::signal(signal, libc::SIG_DFL);
        let mut signals = std::mem::zeroed();
        libc::sigemptyset(&mut signals);
        libc::sigaddset(&mut signals, signal);
        libc::pthread_sigmask(libc::SIG_UNBLOCK, &signals, std::ptr::null_mut());
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{os::unix::process::ExitStatusExt, process::Command};

    #[test]
    fn ordinary_status_is_preserved() {
        assert_eq!(child_code(ExitStatus::from_raw(0)).unwrap(), 0);
        assert_eq!(child_code(ExitStatus::from_raw(7 << 8)).unwrap(), 7);
        let signal = child_code(ExitStatus::from_raw(libc::SIGTERM)).unwrap_err();
        assert_eq!(signal.to_string(), "child terminated by signal 15");
    }

    #[test]
    fn signal_is_reemitted_even_if_inherited_ignored_and_blocked() {
        if let Ok(signal) = std::env::var("OTELC_TEST_CHILD_SIGNAL") {
            let signal: i32 = signal.parse().unwrap();
            // SAFETY: isolated subprocess; no application signal state is changed.
            unsafe {
                libc::signal(signal, libc::SIG_IGN);
                let mut signals = std::mem::zeroed();
                libc::sigemptyset(&mut signals);
                libc::sigaddset(&mut signals, signal);
                libc::pthread_sigmask(libc::SIG_BLOCK, &signals, std::ptr::null_mut());
            }
            if std::env::var_os("OTELC_TEST_PREPARE_SIGNAL").is_some() {
                default_unblocked(signal);
                // SAFETY: inspect this isolated child's handler and thread mask.
                unsafe {
                    let mut mask = std::mem::zeroed();
                    assert_eq!(
                        libc::pthread_sigmask(libc::SIG_BLOCK, std::ptr::null(), &mut mask),
                        0
                    );
                    assert_eq!(libc::sigismember(&mask, signal), 0);
                    if signal != libc::SIGKILL {
                        let mut action: libc::sigaction = std::mem::zeroed();
                        assert_eq!(libc::sigaction(signal, std::ptr::null(), &mut action), 0);
                        assert_eq!(action.sa_sigaction, libc::SIG_DFL);
                    }
                }
                return;
            }
            child_code(ExitStatus::from_raw(signal))
                .unwrap_err()
                .terminate();
        }
        for signal in [libc::SIGTERM, libc::SIGKILL] {
            let prepared = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "tests::signal_is_reemitted_even_if_inherited_ignored_and_blocked",
                ])
                .env("OTELC_TEST_CHILD_SIGNAL", signal.to_string())
                .env("OTELC_TEST_PREPARE_SIGNAL", "1")
                .output()
                .unwrap();
            assert!(
                prepared.status.success(),
                "signal {signal}: {}",
                String::from_utf8_lossy(&prepared.stdout)
            );
            let output = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "tests::signal_is_reemitted_even_if_inherited_ignored_and_blocked",
                ])
                .env("OTELC_TEST_CHILD_SIGNAL", signal.to_string())
                .output()
                .unwrap();
            assert_eq!(output.status.signal(), Some(signal));
        }
    }
}
