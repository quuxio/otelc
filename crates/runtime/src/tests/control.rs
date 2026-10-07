use super::*;
use std::os::unix::fs::DirBuilderExt;
#[test]
fn commands_are_explicit_and_invalid_requests_leave_state_unchanged() {
    let enabled = AtomicBool::new(false);
    apply("status", &enabled).unwrap();
    assert!(!enabled.load(Ordering::Acquire));
    apply("enable", &enabled).unwrap();
    assert!(enabled.load(Ordering::Acquire));
    assert!(apply("unknown", &enabled).is_err());
    assert!(enabled.load(Ordering::Acquire));
    apply("disable", &enabled).unwrap();
    assert!(!enabled.load(Ordering::Acquire));
}
#[test]
fn bind_requires_private_directory_and_preserves_occupied_paths() {
    let directory = std::env::temp_dir().join(format!("otelc-control-test-{}", std::process::id()));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&directory)
        .unwrap();
    let path = directory.join("metrics.sock");
    std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(bind(path.to_str().unwrap()).is_err());
    std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700)).unwrap();
    std::fs::write(&path, b"preserve").unwrap();
    assert!(bind(path.to_str().unwrap()).is_err());
    assert_eq!(std::fs::read(&path).unwrap(), b"preserve");
    std::fs::remove_file(&path).unwrap();
    let bound = bind(path.to_str().unwrap()).unwrap();
    assert_eq!(std::fs::metadata(&path).unwrap().mode() & 0o777, 0o600);
    assert!(bind(path.to_str().unwrap()).is_err());
    drop(bound);
    assert!(!path.exists());
    std::fs::remove_dir(&directory).unwrap();
}
#[test]
fn request_framing_is_bounded_and_requires_a_complete_line() {
    for (bytes, valid) in [
        (b"status\n".as_slice(), true),
        (b"012345678901234567\n".as_slice(), false),
        (b"partial".as_slice(), false),
        (b"\xff\n".as_slice(), false),
    ] {
        let (mut client, mut server) = UnixStream::pair().unwrap();
        client.write_all(bytes).unwrap();
        client.shutdown(std::net::Shutdown::Write).unwrap();
        assert_eq!(read_command(&mut server).is_ok(), valid);
    }
    let (_client, mut server) = UnixStream::pair().unwrap();
    let started = Instant::now();
    assert!(read_command(&mut server).is_err());
    assert!(started.elapsed() < Duration::from_secs(1));
}
