use super::*;
use std::{
    io::{Read, Write},
    net::TcpListener,
    thread,
};
fn response_server(responses: Vec<(u16, Vec<u8>)>) -> (String, thread::JoinHandle<usize>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}/v1/metrics", listener.local_addr().unwrap());
    let handle = thread::spawn(move || {
        for (code, body) in &responses {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut header = vec![];
            let mut one = [0];
            while !header.ends_with(b"\r\n\r\n") {
                stream.read_exact(&mut one).unwrap();
                header.push(one[0]);
            }
            let header = String::from_utf8(header).unwrap();
            let count = header
                .lines()
                .find_map(|s| {
                    s.to_lowercase()
                        .strip_prefix("content-length:")
                        .map(|v| v.trim().parse::<usize>().unwrap())
                })
                .unwrap();
            let mut payload = vec![0; count];
            stream.read_exact(&mut payload).unwrap();
            write!(stream,"HTTP/1.1 {code} Result\r\nContent-Type: application/x-protobuf\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",body.len()).unwrap();
            stream.write_all(body).unwrap();
        }
        responses.len()
    });
    (endpoint, handle)
}
#[test]
fn accepted_response() {
    let (endpoint, handle) = response_server(vec![(200, vec![])]);
    send(&endpoint, &BTreeMap::new(), b"test", Duration::from_secs(1)).unwrap();
    assert_eq!(handle.join().unwrap(), 1);
}
#[test]
fn transient_retry() {
    let (endpoint, handle) = response_server(vec![(503, vec![]), (200, vec![])]);
    send(&endpoint, &BTreeMap::new(), b"test", Duration::from_secs(1)).unwrap();
    assert_eq!(handle.join().unwrap(), 2);
}
#[test]
fn permanent_failure_no_retry() {
    let (endpoint, handle) = response_server(vec![(400, vec![])]);
    assert!(send(&endpoint, &BTreeMap::new(), b"test", Duration::from_secs(1)).is_err());
    assert_eq!(handle.join().unwrap(), 1);
}
#[test]
fn partial_success_no_retry() {
    use opentelemetry_proto::tonic::collector::metrics::v1::ExportMetricsPartialSuccess;
    let body = ExportMetricsServiceResponse {
        partial_success: Some(ExportMetricsPartialSuccess {
            rejected_data_points: 1,
            error_message: "private error must not be logged".into(),
        }),
    }
    .encode_to_vec();
    let (endpoint, handle) = response_server(vec![(200, body)]);
    let error = send(&endpoint, &BTreeMap::new(), b"test", Duration::from_secs(1)).unwrap_err();
    assert!(!error.to_string().contains("private"));
    assert_eq!(handle.join().unwrap(), 1);
}
#[test]
fn invalid_response_no_retry() {
    let (endpoint, handle) = response_server(vec![(200, vec![255])]);
    assert!(send(&endpoint, &BTreeMap::new(), b"test", Duration::from_secs(1)).is_err());
    assert_eq!(handle.join().unwrap(), 1);
}
