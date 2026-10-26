"""Retain bounded original HTTP entities before content/protobuf decoding."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from test_java_integration import Capture


class CaptureRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def capture(self) -> Capture:
        capture = Capture(self.root / 'requests')
        self.addCleanup(capture.close)
        return capture

    def test_real_concurrent_http_keeps_exact_entities_before_gzip_and_protobuf_decoding(self) -> None:
        capture = self.capture()
        capture.trace_response_code = 503
        messages = []
        for kind, cls in (('metrics', ExportMetricsServiceRequest), ('traces', ExportTraceServiceRequest)):
            plain = cls().SerializeToString()
            messages.extend([(kind, '', plain), (kind, 'gzip', gzip.compress(plain, mtime=0))])

        def send(row: tuple[str, str, bytes]) -> None:
            kind, encoding, raw = row
            headers = {'Content-Type': 'application/x-protobuf', 'X-Request-Note': 'omit-this-header'}
            if encoding:
                headers['Content-Encoding'] = encoding
            request = Request(f'http://127.0.0.1:{capture.server.server_port}/v1/{kind}',
                              data=raw, headers=headers)
            try:
                with urlopen(request, timeout=5) as response:
                    self.assertEqual((kind, response.status), ('metrics', 200))
            except HTTPError as error:
                self.assertEqual((kind, error.code), ('traces', 503))
                error.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(send, messages))
        paths = sorted((self.root / 'requests').glob('otlp-*.json'))
        self.assertEqual(len(paths), 4)
        observed = []
        for index, path in enumerate(paths):
            metadata = json.loads(path.read_text())
            raw = path.with_suffix('.http-body').read_bytes()
            self.assertEqual(metadata['sequence'], index)
            self.assertEqual(metadata['body_bytes'], len(raw))
            self.assertEqual(metadata['body_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(set(metadata), {'schema_version', 'sequence', 'received_at_unix_ns', 'path',
                                            'content_encoding', 'body_bytes', 'body_sha256', 'scope'})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            observed.append((metadata['path'].removeprefix('/v1/'), metadata['content_encoding'], raw))
        self.assertCountEqual(observed, messages)
        self.assertEqual((len(capture.metrics), len(capture.traces)), (2, 2))
        self.assertEqual(stat.S_IMODE((self.root / 'requests').stat().st_mode), 0o700)

    def test_default_receiver_still_accepts_requests_without_recording(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        request = Request(f'http://127.0.0.1:{capture.server.server_port}/v1/metrics', data=b'',
                          headers={'Content-Type': 'application/x-protobuf'})
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(capture.metrics), 1)
        self.assertIsNone(capture.record_directory)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_chunked_http_keeps_the_original_compressed_entity_without_framing(self) -> None:
        capture = self.capture()
        payload = gzip.compress(ExportMetricsServiceRequest().SerializeToString(), mtime=0)
        connection = HTTPConnection('127.0.0.1', capture.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request('POST', '/v1/metrics', body=iter((payload[:5], payload[5:])),
            headers={'Content-Type': 'application/x-protobuf', 'Content-Encoding': 'gzip',
                     'Transfer-Encoding': 'chunked'}, encode_chunked=True)
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b''))
        path = self.root / 'requests/otlp-000000.http-body'
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(json.loads(path.with_suffix('.json').read_text())['content_encoding'], 'gzip')
        self.assertEqual(len(capture.metrics), 1)

    def test_retention_budget_refuses_before_creating_partial_records(self) -> None:
        capture = self.capture()
        with patch.object(capture, 'MAX_RETAINED_BYTES', 1), self.assertRaises(ValueError):
            capture.retain_request('/v1/metrics', '', b'12')
        with patch.object(capture, 'MAX_RETAINED_REQUESTS', 0), self.assertRaises(ValueError):
            capture.retain_request('/v1/traces', '', b'')
        self.assertEqual(list((self.root / 'requests').iterdir()), [])

