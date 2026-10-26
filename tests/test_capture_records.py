"""Retain bounded OTLP entities and preserve benchmark control failures."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from otelc import control, native_benchmark
from test_java_integration import Capture
import test_native_benchmark as native_tests


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

    def test_retention_budget_refuses_before_creating_partial_records(self) -> None:
        capture = self.capture()
        with patch.object(capture, 'MAX_RETAINED_BYTES', 1), self.assertRaises(ValueError):
            capture.retain_request('/v1/metrics', '', b'12')
        with patch.object(capture, 'MAX_RETAINED_REQUESTS', 0), self.assertRaises(ValueError):
            capture.retain_request('/v1/traces', '', b'')
        self.assertEqual(list((self.root / 'requests').iterdir()), [])


class BenchmarkFailureCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.case = native_tests.NativeBenchmarkIntegrationTests('test_cpp_lifetime_cost_uses_the_common_policy_and_true_destruction')
        self.case.base = self.root
        self.case.sdk = Path('/unused-sdk')
        self.case.capture_diagnostics = True
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def attempt(self, *, health_error: bool = False, create_output: bool = True) -> tuple[Exception, dict | None]:
        failure = ValueError('original flush failure')

        self.request_commands = []

        def request(endpoint: Path, command: str) -> dict:
            self.request_commands.append(command)
            if command == 'flush':
                raise failure
            self.assertEqual(command, 'health')
            if health_error:
                raise RuntimeError('do-not-record-this-message')
            return {'phase': 'running', 'metric_flush': {'failed_attempts': '1'}}

        def run(*args, **kwargs) -> None:
            output = args[1]
            if create_output:
                output.mkdir()
            control.request(output / 'control.json', 'flush')

        with patch.object(control, 'request', side_effect=request), \
                patch.object(native_benchmark, 'run', side_effect=run), self.assertRaises(ValueError) as caught:
            self.case.run_benchmark('cpp')
        self.assertIs(caught.exception, failure)
        path = self.root / (self.case._testMethodName + '-cpp') / 'failed-flush.json'
        return failure, json.loads(path.read_text()) if path.exists() else None

    def test_low_space_refuses_before_sdk_setup(self) -> None:
        with patch.object(native_tests.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)), \
                patch.object(native_tests.native_launch, 'build_sdk') as build, \
                self.assertRaisesRegex(ValueError, '8 GiB'):
            native_tests.NativeBenchmarkIntegrationTests.setUpClass()
        build.assert_not_called()

    def test_failed_flush_keeps_immediate_health_and_the_original_failure(self) -> None:
        _, value = self.attempt()
        self.assertEqual(value['health'], {'phase': 'running', 'metric_flush': {'failed_attempts': '1'}})
        self.assertEqual(value['command'], 'flush')

    def test_unavailable_health_records_only_its_type_and_preserves_the_original_failure(self) -> None:
        _, value = self.attempt(health_error=True)
        self.assertEqual(value['health_error_type'], 'RuntimeError')
        self.assertNotIn('do-not-record-this-message', json.dumps(value))

    def test_disabled_capture_does_not_add_control_calls(self) -> None:
        self.case.capture_diagnostics = False
        _, value = self.attempt()
        self.assertIsNone(value)
        self.assertEqual(self.request_commands, ['flush'])

    def test_recording_failure_cannot_replace_the_original_control_failure(self) -> None:
        failure, value = self.attempt(create_output=False)
        self.assertIsNone(value)
        self.assertEqual(failure.__notes__, ['Native benchmark diagnostic capture failed: FileNotFoundError'])
