//! Decode actual SDK wire messages independently of runtime health counters.
use opentelemetry_proto::tonic::{
    collector::trace::v1::ExportTraceServiceRequest, trace::v1::Span,
};
use prost::Message;
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::Duration,
};

pub struct Receiver {
    pub endpoint: String,
    address: std::net::SocketAddr,
    stopped: Arc<AtomicBool>,
    traces: Arc<Mutex<Vec<ExportTraceServiceRequest>>>,
    worker: Option<JoinHandle<()>>,
}
impl Receiver {
    pub fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let stopped = Arc::new(AtomicBool::new(false));
        let traces = Arc::new(Mutex::new(vec![]));
        let (stop, received) = (stopped.clone(), traces.clone());
        let worker = thread::spawn(move || {
            for incoming in listener.incoming() {
                let mut stream = incoming.unwrap();
                if stop.load(Ordering::Acquire) {
                    break;
                }
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut headers = vec![];
                let mut byte = [0];
                while !headers.ends_with(b"\r\n\r\n") && headers.len() < 65536 {
                    if stream.read_exact(&mut byte).is_err() {
                        break;
                    }
                    headers.push(byte[0]);
                }
                if !headers.ends_with(b"\r\n\r\n") {
                    continue;
                }
                let headers = String::from_utf8(headers).unwrap();
                let length = headers
                    .lines()
                    .filter_map(|line| line.split_once(':'))
                    .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
                    .map(|(_, value)| value.trim().parse::<usize>().unwrap())
                    .unwrap_or(0);
                assert!(length <= 4 * 1024 * 1024);
                let mut body = vec![0; length];
                if stream.read_exact(&mut body).is_err() {
                    continue;
                }
                if headers.starts_with("POST /v1/traces ") {
                    received
                        .lock()
                        .unwrap()
                        .push(ExportTraceServiceRequest::decode(&*body).unwrap());
                }
                let _ = stream.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                );
            }
        });
        Self {
            endpoint: format!("http://{address}"),
            address,
            stopped,
            traces,
            worker: Some(worker),
        }
    }
    pub fn spans(&self) -> Vec<Span> {
        let spans: Vec<_> = self
            .traces
            .lock()
            .unwrap()
            .iter()
            .flat_map(|request| &request.resource_spans)
            .flat_map(|resource| &resource.scope_spans)
            .flat_map(|scope| &scope.spans)
            .cloned()
            .collect();
        let nodes: std::collections::BTreeMap<_, _> = spans
            .iter()
            .map(|span| ((span.trace_id.clone(), span.span_id.clone()), span))
            .collect();
        assert_eq!(nodes.len(), spans.len(), "duplicate wire span identity");
        for span in &spans {
            assert_eq!(span.trace_id.len(), 16);
            assert!(span.trace_id.iter().any(|byte| *byte != 0));
            assert_eq!(span.span_id.len(), 8);
            assert!(span.span_id.iter().any(|byte| *byte != 0));
            assert!(
                span.end_time_unix_nano >= span.start_time_unix_nano
                    && span.start_time_unix_nano > 0
            );
            let mut node = span;
            let mut visited = std::collections::BTreeSet::new();
            while !node.parent_span_id.is_empty() {
                assert!(visited.insert(node.span_id.clone()), "cyclic wire trace");
                let parent = nodes
                    .get(&(node.trace_id.clone(), node.parent_span_id.clone()))
                    .expect("missing wire parent");
                assert!(
                    parent.start_time_unix_nano <= node.start_time_unix_nano
                        && parent.end_time_unix_nano >= node.end_time_unix_nano
                );
                node = parent;
            }
        }
        spans
    }
}
impl Drop for Receiver {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Release);
        let _ = TcpStream::connect(self.address);
        self.worker.take().unwrap().join().unwrap();
    }
}
