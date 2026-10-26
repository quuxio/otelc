"""Actual query routes must retain complete current data or fail visibly."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

from ci import go_benchmark_downstream as downstream
import test_go_benchmark_receipt as fixtures


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
        with patch.object(downstream.urllib.request, 'urlopen', side_effect=self.response):
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
        with patch.object(downstream.urllib.request, 'urlopen', side_effect=self.response), \
                self.assertRaisesRegex(ValueError, 'provisioned source'):
            downstream.check(self.fixture.value, self.output)
        self.assertFalse((self.output / 'receipt.json').exists())

    def test_response_size_and_object_shape_are_bounded(self) -> None:
        for payload in (b'x' * (downstream.LIMIT + 1), b'[]'):
            with patch.object(downstream.urllib.request, 'urlopen', return_value=io.BytesIO(payload)), \
                    self.assertRaises(ValueError):
                downstream.request('http://127.0.0.1:9091/api/v1/query')


if __name__ == '__main__':
    unittest.main()
