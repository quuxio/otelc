"""Verify the current Go comparison in Prometheus and Grafana's real datasource.

This checks backend data and the provisioned mean-panel expression. It does not
replace visual dashboard inspection, trace acceptance or oteleq qualification.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
import urllib.parse
import urllib.request
import urllib.error

from ci import go_benchmark_receipt as receipt

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 2 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A different service is not the declared local stack, even after HTTP 302."""

    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def open_local(value: str | urllib.request.Request, *, timeout: float):
    """Use only literal loopback HTTP, without environment proxies or redirects."""
    url = value.full_url if isinstance(value, urllib.request.Request) else value
    receipt.require(type(url) is str and not any(ord(char) <= 32 for char in url),
                    'benchmark stack requires a literal loopback HTTP URL')
    parsed = urllib.parse.urlsplit(url)
    receipt.require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                    and parsed.username is None and parsed.password is None
                    and not parsed.fragment and parsed.port is not None
                    and 1 <= parsed.port <= 65535,
                    'benchmark stack requires a literal loopback HTTP URL')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        return opener.open(value, timeout=timeout)
    except urllib.error.HTTPError as error:
        error.close()
        raise


def request(url: str) -> tuple[dict[str, Any], bytes]:
    """Use the declared loopback stack and bound each actual JSON response."""
    with open_local(url, timeout=3) as response:
        raw = response.read(LIMIT + 1)
    receipt.require(len(raw) <= LIMIT, 'benchmark downstream response exceeds 2 MiB')
    value = json.loads(raw)
    receipt.require(type(value) is dict, 'benchmark downstream response is not an object')
    return value, raw


def save(directory: Path, name: str, raw: bytes) -> str:
    """Retain the original response bytes once; never repair an earlier record."""
    path = directory / name
    with path.open('xb') as output:
        path.chmod(0o600)
        output.write(raw)
    return hashlib.sha256(raw).hexdigest()


def measured(url: str, value: dict[str, Any], directory: Path, label: str, *,
             counts: bool, aggregated: bool = False, timeout: float = 45.) -> dict:
    """Await actual scrape data without restarting or rerunning the benchmark."""
    deadline = time.monotonic() + timeout
    attempts, raw, failure = 0, b'', ''
    while True:
        attempts += 1
        try:
            document, raw = request(url)
            result = receipt.vector(document, value, counts=counts, aggregated=aggregated)
            break
        except (OSError, ValueError) as error:
            failure = f'{type(error).__name__}: {error}'
            if time.monotonic() >= deadline:
                if raw:
                    save(directory, label + '-failed-response.json', raw)
                save(directory, label + '-failure.json', json.dumps({
                    'url': url, 'attempts': attempts, 'error': failure}).encode())
                raise ValueError('current benchmark metrics were not available downstream: ' + failure) from error
            time.sleep(.25)
    digest = save(directory, label + '-response.json', raw)
    return {'url': url, 'attempts': attempts, 'response_sha256': digest, 'values': result}


def check(value: dict[str, Any], directory: Path) -> dict:
    """Join all current-run counts before evaluating the actual Grafana panel."""
    comparison = value['comparison_id']
    receipt.require(type(comparison) is str and re.fullmatch('[a-z0-9_-]{1,128}', comparison) is not None,
                    'retained qualification needs an unambiguous comparison identifier')
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    selector = 'language="go",comparison_id=' + json.dumps(comparison)
    expression = 'otelc_benchmark_operation_duration_seconds_count{' + selector + '}'
    # Deliberately omit run_id from the query: a second run using the same
    # comparison ID must be visible as extra/stale series and fail the join.
    counts = measured('http://127.0.0.1:9091/api/v1/query?' + urllib.parse.urlencode({'query': expression}),
                      value, directory, 'prometheus-counts', counts=True)
    dashboard_url = 'http://127.0.0.1:3001/api/dashboards/uid/otelc-comparison'
    dashboard, raw = request(dashboard_url)
    dashboard_hash = save(directory, 'grafana-dashboard.json', raw)
    actual = dashboard['dashboard']
    expected = json.loads((ROOT / 'deploy/grafana/dashboards/comparison.json').read_bytes())
    actual_panel, = [row for row in actual['panels'] if row['id'] == 2]
    expected_panel, = [row for row in expected['panels'] if row['id'] == 2]
    receipt.require(actual['uid'] == expected['uid'] == 'otelc-comparison'
                    and receipt.same(actual_panel['targets'], expected_panel['targets'])
                    and receipt.same(actual_panel['datasource'], expected_panel['datasource']),
                    'running Grafana mean panel differs from its current provisioned source')
    expression = actual_panel['targets'][0]['expr'].replace('$comparison_id', comparison).replace('$__range', '1h')
    receipt.require('$' not in expression, 'unresolved Grafana benchmark variable')
    mean_url = 'http://127.0.0.1:3001/api/datasources/proxy/uid/prometheus/api/v1/query?'
    means = measured(mean_url + urllib.parse.urlencode({'query': expression}), value, directory,
                     'grafana-means', counts=False, aggregated=True)
    result = {'comparison_id': comparison, 'run_id': value['run_id'],
              'prometheus_counts': counts, 'grafana_mean_panel': means,
              'grafana_dashboard_response_sha256': dashboard_hash,
              'scope': 'Actual current-run scrape counts and Grafana mean-panel datasource results; visual rendering and trace qualification remain separate'}
    save(directory, 'receipt.json', (json.dumps(result, indent=2) + '\n').encode())
    return result
