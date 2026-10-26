"""Actual query routes must retain complete current data or fail visibly."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from ci import go_benchmark_downstream as downstream
import test_go_benchmark_receipt as fixtures


@contextmanager
def loopback(status: int, body: bytes, *, location: str | None = None):
    """Two owned test endpoints exercise routing without any external network."""
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.response()

        def do_POST(self):
            calls.append({'path': self.path, 'body': self.rfile.read(int(self.headers['Content-Length'])),
                          'content-type': self.headers['Content-Type']})
            self.response()

        def response(self):
            self.send_response(status)
            if location is not None:
                self.send_header('Location', location)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # The test retains exact paths in calls instead of stderr.
    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class GoBenchmarkDownstreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.GoBenchmarkReceiptTests()
        self.fixture.setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / 'evidence'
        self.urls = []
        dashboard = json.loads((downstream.ROOT / 'deploy/grafana/dashboards/comparison.json').read_text())
        self.dashboard = {'dashboard': dashboard}

    def response(self, url: str, **kwargs: object) -> io.BytesIO:
        self.urls.append(url)
        self.assertEqual(kwargs['timeout'], 3)
        if '/api/dashboards/' in url:
            value = self.dashboard
        elif ':9091/' in url:
            value = self.fixture.vector(counts=True)
        else:
            value = self.fixture.vector(counts=False, aggregated=True)
        return io.BytesIO((json.dumps(value) + '\n').encode())

    def test_real_query_routes_and_original_responses_are_retained(self) -> None:
        with patch.object(downstream, 'open_local', side_effect=self.response):
            value = downstream.check(self.fixture.value, self.output)
        self.assertEqual(len(self.urls), 3)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.urls[0]).query)['query'][0]
        self.assertIn('comparison_id="fresh-run"', query)
        self.assertNotIn('run_id=', query)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.urls[2]).query)['query'][0]
        self.assertNotIn('$', query)
        self.assertIn('last_over_time', query)
        self.assertEqual(value['prometheus_counts']['values']['plain'], 4)
        self.assertEqual(len(list(self.output.iterdir())), 4)
        self.assertEqual((self.output / 'grafana-dashboard.json').read_bytes(),
                         (json.dumps(self.dashboard) + '\n').encode())
        with self.assertRaises(FileExistsError):
            downstream.save(self.output, 'receipt.json', b'changed')

    def test_partial_scrape_is_retried_without_restarting_an_application(self) -> None:
        self.output.mkdir()
        empty = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
        complete = self.fixture.vector(counts=True)
        with patch.object(downstream, 'request', side_effect=[(empty, b'{}'), (complete, b'original')]), \
                patch.object(downstream.time, 'sleep') as sleep:
            value = downstream.measured('http://127.0.0.1:9091/api/v1/query', self.fixture.value,
                                       self.output, 'counts', counts=True)
        self.assertEqual(value['attempts'], 2)
        sleep.assert_called_once_with(.25)
        self.assertEqual((self.output / 'counts-response.json').read_bytes(), b'original')

    def test_deadline_keeps_last_actual_response_and_no_success_receipt(self) -> None:
        self.output.mkdir()
        value = self.fixture.vector(counts=True)
        value['data']['result'].pop()
        with patch.object(downstream, 'request', return_value=(value, b'last original response')), \
                self.assertRaisesRegex(ValueError, 'not available downstream'):
            downstream.measured('http://127.0.0.1:9091/api/v1/query', self.fixture.value,
                                self.output, 'counts', counts=True, timeout=0)
        self.assertEqual((self.output / 'counts-failed-response.json').read_bytes(), b'last original response')
        self.assertFalse((self.output / 'counts-response.json').exists())

    def test_unavailable_stack_preserves_failure_without_inventing_a_response(self) -> None:
        self.output.mkdir()
        with patch.object(downstream, 'request', side_effect=OSError('unavailable')), \
                self.assertRaises(ValueError):
            downstream.measured('http://127.0.0.1:9091/api/v1/query', self.fixture.value,
                                self.output, 'counts', counts=True, timeout=0)
        self.assertEqual(len(list(self.output.iterdir())), 1)
        self.assertIn('unavailable', (self.output / 'counts-failure.json').read_text())

    def test_changed_dashboard_or_comparison_identifier_cannot_publish_success(self) -> None:
        for identity in ('old.run', '"quoted"', '', True):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                downstream.check(self.fixture.value | {'comparison_id': identity}, self.output)
        self.assertFalse(self.output.exists())
        self.dashboard['dashboard']['panels'][1]['targets'][0]['expr'] = 'up'
        with patch.object(downstream, 'open_local', side_effect=self.response), \
                self.assertRaisesRegex(ValueError, 'provisioned source'):
            downstream.check(self.fixture.value, self.output)
        self.assertFalse((self.output / 'receipt.json').exists())

    def test_response_size_and_object_shape_are_bounded(self) -> None:
        for payload in (b'x' * (downstream.LIMIT + 1), b'[]'):
            with patch.object(downstream, 'open_local', return_value=io.BytesIO(payload)), \
                    self.assertRaises(ValueError):
                downstream.request('http://127.0.0.1:9091/api/v1/query')

    def test_environment_proxy_cannot_substitute_the_declared_local_stack(self) -> None:
        with loopback(200, b'{"source":"target"}') as (target, target_calls), \
                loopback(200, b'{"source":"proxy"}') as (proxy, proxy_calls), \
                patch.dict(os.environ, {'http_proxy': proxy, 'HTTP_PROXY': proxy,
                    'https_proxy': proxy, 'HTTPS_PROXY': proxy, 'ALL_PROXY': proxy,
                    'all_proxy': proxy, 'no_proxy': '', 'NO_PROXY': ''}):
            value, raw = downstream.request(target + '/data')
        self.assertEqual(value, {'source': 'target'})
        self.assertEqual(target_calls, ['/data'])
        self.assertEqual(proxy_calls, [])

    def test_redirect_response_cannot_replace_the_selected_service(self) -> None:
        with loopback(200, b'{"source":"redirected"}') as (target, target_calls), \
                loopback(302, b'', location=target + '/other') as (redirect, _), \
                patch.dict(os.environ, {'no_proxy': '*', 'NO_PROXY': '*'}), \
                self.assertRaises(urllib.error.HTTPError) as caught:
            downstream.request(redirect + '/data')
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(target_calls, [])

    def test_readiness_post_uses_the_same_direct_loopback_transport(self) -> None:
        with loopback(200, b'{}') as (target, calls), \
                loopback(200, b'{"source":"proxy"}') as (proxy, proxy_calls), \
                patch.dict(os.environ, {'http_proxy': proxy, 'HTTP_PROXY': proxy,
                                      'no_proxy': '', 'NO_PROXY': ''}):
            request = downstream.urllib.request.Request(target + '/v1/metrics', data=b'',
                headers={'Content-Type': 'application/x-protobuf'})
            with downstream.open_local(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b'{}')
        self.assertEqual(calls, [{'path': '/v1/metrics', 'body': b'', 'content-type': 'application/x-protobuf'}])
        self.assertEqual(proxy_calls, [])

    def test_foreign_or_ambiguous_urls_refuse_before_opening_any_connection(self) -> None:
        for url in (None, {}, 'https://127.0.0.1:3001/a', 'http://localhost:3001/a',
                    'http://127.0.0.1/a', 'http://127.0.0.1:0/a', 'http://name@127.0.0.1:3001/a',
                    'http://127.0.0.1:3001/a#other', 'http://127.0.0.1:3001/a b'):
            with self.subTest(url=url), \
                    patch.object(downstream.urllib.request, 'build_opener', side_effect=AssertionError('must not open')), \
                    self.assertRaises(ValueError):
                downstream.open_local(url, timeout=2)


if __name__ == '__main__':
    unittest.main()
