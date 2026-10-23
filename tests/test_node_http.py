"""Ordinary client/server programs use common config for real service propagation."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import ssl
import shutil
import unittest
import urllib.request
from contextlib import ExitStack
from pathlib import Path

from otelc import control, node
from otelc.config import Policy
from test_java_context import calls
from test_java_http_context import Downstream, histogram_count, stop
from test_java_integration import Capture, batch, wait_for_count

ROOT = Path(__file__).resolve().parent.parent


def policy_for(capture: Capture, enabled: bool) -> Policy:
    """Apply one unchanged policy to JS and both Microsoft compiler paths."""
    data = copy.deepcopy(Policy.load(ROOT / 'examples/otelc.toml').data)
    data['context'].update(http=enabled, tasks=False)
    data['functions']['include'] = ['*.workloadRequest', '*.workloadHandle']
    data['metrics'].update(collection=False, emission=False)
    data['traces']['sample_ratio'] = 1.
    data['lifetimes']['enabled'] = False
    data['export'].update(endpoint=f'http://127.0.0.1:{capture.server.server_port}', interval_ms=60000)
    return Policy.parse(data)


def spans(capture: Capture):
    """Read independently decoded spans, not an exporter acknowledgement."""
    return [span for message in capture.traces for resource in message.resource_spans
            for scope in resource.scope_spans for span in scope.spans]


class NodeHttpTests(unittest.TestCase):
    def exercise(self, language: str, backend: str, enabled: bool, shape: str = 'object', *,
                 ratio: float = 1., tracing: bool = True, excluded: bool = False, tls: bool = False) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        downstream = Downstream()
        self.addCleanup(downstream.close)
        data = copy.deepcopy(policy_for(capture, enabled).data)
        data['traces'].update(enabled=tracing, sample_ratio=ratio)
        if excluded:
            data['sources']['exclude'] = ['**']
        policy = Policy.parse(data)
        source = ROOT / 'examples' / language / ('HttpContextApp.ts' if language == 'typescript' else 'HttpContextApp.js')
        original = source.read_bytes()
        outcomes = []
        audits = []
        reports = []
        with tempfile.TemporaryDirectory(prefix='otelc-node-http-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            certificate = root / 'server.crt'
            private_key = root / 'server.key'
            tls_context = None
            if tls:
                homebrew = Path('/opt/homebrew/opt/openssl@3/bin/openssl')
                openssl = str(homebrew) if homebrew.is_file() else shutil.which('openssl')
                self.assertIsNotNone(openssl)
                subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(private_key), '-out', str(certificate), '-days', '1', '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=IP:127.0.0.1'], capture_output=True, check=True, timeout=20)
                tls_context = ssl.create_default_context(cafile=str(certificate))
            for plain in (True, False):
                lane = root / ('plain' if plain else 'instrumented')
                audit = lane / 'requests.jsonl'
                with ExitStack() as stack:
                    def launch(role: str, argument: str):
                        runtime = lane / role
                        argv = [*node.command(policy, runtime, language, plain=plain, backend=backend, entry=source),
                            role, argument, f'http://127.0.0.1:{downstream.server.server_port}/sink']
                        if tls:
                            argv.extend([str(certificate), str(private_key)])
                        process = stack.enter_context(subprocess.Popen(argv, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                        stack.callback(stop, process)
                        return process, runtime

                    server, server_runtime = launch('server', str(audit))
                    ready = json.loads(server.stdout.readline())
                    endpoint = ('https' if tls else 'http') + '://127.0.0.1:' + str(ready['port'])
                    client, client_runtime = launch('client', endpoint)
                    self.assertEqual(json.loads(client.stdout.readline()), {'ready': True})
                    if not plain:
                        control.wait_ready(server_runtime / 'control.json', server.pid)
                        control.wait_ready(client_runtime / 'control.json', client.pid)
                    replies = []
                    for index, (collection, emission) in enumerate(((False, False), (True, False), (False, True), (True, True))):
                        if not plain:
                            for process, runtime in ((server, server_runtime), (client, client_runtime)):
                                control.request(runtime / 'control.json', 'collection ' + ('on' if collection else 'off'))
                                status = control.request(runtime / 'control.json', 'emission ' + ('on' if emission else 'off'))
                                self.assertEqual((status['pid'], status['collection'], status['emission']),
                                                 (process.pid, collection, emission))
                        for path in ('/ok', '/error'):
                            replies.append(batch(client, json.dumps({'path': path, 'shape': shape})))
                        for sampled in (True, False):
                            trace = f'{index + (1 if sampled else 101):032x}'
                            parent = f'{index + 1:016x}'
                            request = urllib.request.Request(endpoint + ('/sampled' if sampled else '/unsampled'),
                                headers={'traceparent': f'00-{trace}-{parent}-0{int(sampled)}',
                                         'baggage': 'session=ordinary', 'X-Business': 'unchanged'})
                            with urllib.request.urlopen(request, timeout=10, context=tls_context) as response:
                                self.assertEqual((response.status, response.read()), (200, b'8'))
                        request = urllib.request.Request(endpoint + '/relay', headers={
                            'traceparent': f'00-{index + 201:032x}-{index + 1:016x}-01', 'baggage': 'session=ordinary'})
                        with urllib.request.urlopen(request, timeout=10, context=tls_context) as response:
                            self.assertEqual((response.status, response.read()), (200, b'8'))
                        self.assertEqual(batch(server, 'barrier'), {'idle': True})
                    if not plain:
                        for runtime in (server_runtime, client_runtime):
                            control.request(runtime / 'control.json', 'flush')
                        self.assertEqual(wait_for_count(lambda: calls(capture), 0 if excluded else 16), 0 if excluded else 16)
                    for process in (client, server):
                        stdout, stderr = process.communicate('quit\n', timeout=15)
                        self.assertEqual((process.returncode, stdout, stderr), (0, '', ''))
                    if not plain:
                        for runtime in (server_runtime, client_runtime):
                            report = node.wait_report(runtime)
                            self.assertEqual(report['phase'], 'closed')
                            self.assertEqual(report['handoff']['lost'], 0)
                            self.assertEqual(report['span_accounting']['queue_dropped'], 0)
                            reports.append(report)
                    outcomes.append(replies)
                    audits.append([json.loads(line) for line in audit.read_text().splitlines()])
            expected = [{'status': code, 'body': body, 'unchanged': True, 'requestInstance': True,
                         'responseInstance': True, 'callbackReceiver': True, 'endIdentity': True}
                        for code, body in ((200, '8'), (500, 'bad'))] * 4
            self.assertEqual(outcomes[0], expected)
            self.assertEqual(outcomes[1], outcomes[0], 'business response, callback/return identity and owned headers must be unchanged')
            self.assertEqual([report['function_completions'] for report in reports],
                             [0, 0] if excluded else [24, 8] if tracing else [12, 4])
            observed = spans(capture)
            clients = {span.span_id: span for span in observed if span.kind == 3}
            servers = [span for span in observed if span.kind == 2]
            expected_counts = (56, 12, 16) if enabled else (32, 0, 0)
            if enabled and ratio == 0:
                expected_counts = (24, 4, 8)
            if excluded:
                expected_counts = (28, 12, 16)
            if not tracing:
                expected_counts = (0, 0, 0)
            actual_counts = (len(observed), len(clients), len(servers))
            self.assertEqual(actual_counts, expected_counts)
            self.assertEqual(len([span for span in servers if span.parent_span_id in clients]),
                             8 if enabled and tracing and ratio else 0)
            self.assertEqual(histogram_count(capture, 'http.client.request.duration'), 6 if enabled else 0)
            self.assertEqual(histogram_count(capture, 'http.server.request.duration'), 10 if enabled else 0)
            for message in capture.metrics:
                for resource in message.resource_metrics:
                    for scope in resource.scope_metrics:
                        if scope.scope.name == '@opentelemetry/instrumentation-http':
                            self.assertEqual(scope.scope.version, '0.223.0')
            if enabled and tracing:
                for index in range(4):
                    trace_id = (index + 1).to_bytes(16, 'big')
                    incoming = [span for span in servers if span.trace_id == trace_id]
                    self.assertEqual(len(incoming), 1)
                    self.assertEqual(incoming[0].parent_span_id, (index + 1).to_bytes(8, 'big'))
                    self.assertFalse(any(span.trace_id == (index + 101).to_bytes(16, 'big') for span in observed))
            business = lambda rows: [[(row['raw'][index], row['raw'][index + 1])
                for index in range(0, len(row['raw']), 2)
                if row['raw'][index].lower() in ('x-business', 'x-repeated')] for row in rows]
            self.assertEqual(business(audits[1]), business(audits[0]))
            for row in audits[1]:
                if row['path'] in ('/ok', '/error'):
                    self.assertEqual(row['headers'].get('x-business'), 'unchanged')
                    self.assertEqual(row['headers'].get('x-repeated'), 'one, two')
                    self.assertEqual('traceparent' in row['headers'], enabled)
            self.assertEqual([row for row in downstream.received[:4]], [(None, None)] * 4)
            for traceparent, baggage in downstream.received[4:]:
                self.assertEqual(baggage, 'session=ordinary' if enabled else None)
                self.assertEqual(traceparent is not None, enabled)
            self.assertEqual(source.read_bytes(), original)

    def test_javascript_service_context_sampling_and_all_live_states(self) -> None:
        for enabled in (False, True):
            with self.subTest(http=enabled):
                self.exercise('javascript', 'native', enabled)

    def test_javascript_raw_header_arrays_preserve_business_headers(self) -> None:
        self.exercise('javascript', 'native', True, 'raw')

    def test_https_raw_headers_parentage_and_metrics_use_a_verified_local_certificate(self) -> None:
        self.exercise('javascript', 'native', True, 'raw', tls=True)

    def test_both_actual_microsoft_compilers_propagate_service_context_and_raw_headers(self) -> None:
        for backend in ('native', 'api'):
            for shape in ('object', 'raw'):
                with self.subTest(compiler=backend, shape=shape):
                    self.exercise('typescript', backend, True, shape)

    def test_root_sampling_off_still_honours_remote_parent_sampling(self) -> None:
        self.exercise('javascript', 'native', True, ratio=0.)

    def test_http_metrics_and_live_controls_work_with_traces_disabled(self) -> None:
        self.exercise('javascript', 'native', True, tracing=False)

    def test_source_exclusion_omits_app_probes_and_retains_explicit_http_instrumentation(self) -> None:
        self.exercise('javascript', 'native', True, excluded=True)

    def test_javascript_network_error_does_not_inspect_original_error_properties(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        policy = policy_for(capture, True)
        source = ROOT / 'examples/javascript/HttpContextApp.js'
        replies = []
        with tempfile.TemporaryDirectory(prefix='otelc-node-error-', dir='/tmp') as directory:
            for plain in (True, False):
                runtime = Path(directory).resolve() / ('plain' if plain else 'instrumented')
                with subprocess.Popen([*node.command(policy, runtime, 'javascript', plain=plain, entry=source),
                        'client', 'http://127.0.0.1:1'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True) as process:
                    try:
                        self.assertEqual(json.loads(process.stdout.readline()), {'ready': True})
                        replies.append(batch(process, json.dumps({'path': '/', 'destroy': True})))
                        stdout, stderr = process.communicate('quit\n', timeout=10)
                        self.assertEqual((process.returncode, stdout, stderr), (0, '', ''))
                    finally:
                        stop(process)
                if not plain:
                    self.assertEqual(node.wait_report(runtime)['phase'], 'closed')
        self.assertTrue(replies[0]['errorIdentity'])
        self.assertEqual(replies[1], replies[0], 'telemetry must not read application error getters')
        clients = [span for span in spans(capture) if span.kind == 3]
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].status.code, 2)
        self.assertEqual(clients[0].status.message, '')
        self.assertEqual(list(clients[0].events), [])
        self.assertIn(('error.type', 'Error'), [(item.key, item.value.string_value) for item in clients[0].attributes])
