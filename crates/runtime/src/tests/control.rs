use super::*;
use std::os::unix::{fs::DirBuilderExt, net::UnixStream};
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
#[test]
fn inherited_nonblocking_socket_waits_for_a_fragmented_valid_request() {
    let (mut client, mut server) = UnixStream::pair().unwrap();
    server.set_nonblocking(true).unwrap();
    let writer = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(25));
        client.write_all(b"sta").unwrap();
        std::thread::sleep(Duration::from_millis(25));
        client.write_all(b"tus\n").unwrap();
    });
    assert_eq!(read_command(&mut server).unwrap(), "status");
    writer.join().unwrap();
}
#[test]
fn trickled_bytes_cannot_extend_the_total_control_deadline() {
    let (mut client, mut server) = UnixStream::pair().unwrap();
    let writer = std::thread::spawn(move || {
        for byte in b"status\n" {
            std::thread::sleep(Duration::from_millis(45));
            if client.write_all(&[*byte]).is_err() {
                break;
            }
        }
    });
    let started = Instant::now();
    assert!(read_command(&mut server).is_err());
    assert!(started.elapsed() < Duration::from_millis(300));
    drop(server);
    writer.join().unwrap();
}
