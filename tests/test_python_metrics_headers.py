"""Enforce signal-specific header precedence at the real metrics HTTP boundary."""
import gzip
import os
from unittest.mock import patch
import unittest
import zlib
import requests

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from test_python_adapter import plan
from test_python_spans import receiver
from quux_otelc_python.telemetry import Runtime, MetricsSession


class MetricsHeaderTests(unittest.TestCase):
    def test_absent_empty_and_nonempty_overrides_with_compressed_exports(self):
        for override in (None, "", "x-specific=signal-marker"):
            for compression in ("none", "gzip", "deflate"):
                with self.subTest(override=override, compression=compression):
                    with receiver() as (endpoint, received), patch.dict(os.environ, {
                        "OTEL_EXPORTER_OTLP_HEADERS": "x-generic=synthetic-marker",
                        "OTEL_EXPORTER_OTLP_METRICS_COMPRESSION": compression,
                    }):
                        if override is None:
                            os.environ.pop("OTEL_EXPORTER_OTLP_METRICS_HEADERS", None)
                        else:
                            os.environ["OTEL_EXPORTER_OTLP_METRICS_HEADERS"] = override
                        policy = plan()
                        policy["metrics_endpoint"] = endpoint + "/v1/metrics"
                        runtime = Runtime(policy)
                        try:
                            runtime.enter(100, "selected")
                            runtime.exit(100, False)
                            runtime.provider.force_flush()
                        finally:
                            runtime.close()
                        self.assertTrue(received)
                        for _, raw_headers, body in received:
                            headers = {key.lower(): value for key, value in raw_headers.items()}
                            expected = {"x-generic": "synthetic-marker"} if override is None else {"x-specific": "signal-marker"} if override else {}
                            actual = {key: value for key, value in headers.items() if key in ("x-generic", "x-specific")}
                            self.assertEqual(actual, expected)
                            self.assertEqual(headers["content-type"], "application/x-protobuf")
                            self.assertEqual(headers.get("content-encoding", "none"), compression)
                            if compression == "gzip":
                                body = gzip.decompress(body)
                            elif compression == "deflate":
                                body = zlib.decompress(body)
                            data = ExportMetricsServiceRequest.FromString(body)
                            self.assertTrue(data.resource_metrics)
                        self.assertEqual(runtime.export_loss, 0)

    def test_compression_fallback_and_redacted_transport_failures(self):
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_COMPRESSION": "gzip"}):
            os.environ.pop("OTEL_EXPORTER_OTLP_METRICS_COMPRESSION", None)
            session = MetricsSession({})
            self.assertEqual(session.compression.value, "gzip")
            failure = requests.RequestException("synthetic private marker")
            with patch.object(requests.Session, "request", side_effect=failure):
                with self.assertRaisesRegex(requests.RequestException, "^OTLP metrics request failed$"):
                    session.post("http://127.0.0.1/v1/metrics", data=b"batch")
            session.close()
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_METRICS_COMPRESSION": "unsupported"}):
            headers = {}
            with self.assertRaisesRegex(ValueError, "^unsupported OTLP metrics compression$"):
                MetricsSession(headers)
