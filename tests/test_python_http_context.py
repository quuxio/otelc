"""Native unchanged urllib/WSGI programs produce cross-service SDK evidence."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import ExitStack
from pathlib import Path

from otelc import control
from test_java_integration import Capture, batch
from test_java_http_context import Downstream, metric_points, stop

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'examples/python/HttpContextApp.py'


class PythonHttpContextTests(unittest.TestCase):
    def test_native_client_server_keep_business_output_and_trace_parentage_across_live_states(self) -> None:
        self.exercise()

    def test_incoming_sampling_tracestate_baggage_and_relay_context(self) -> None:
        self.exercise(inbound=True)

    def test_incoming_sampled_parent_is_honoured_with_zero_root_sampling(self) -> None:
        self.exercise(inbound=True, ratio=0.)

    def test_tracing_disabled_keeps_http_metrics_and_business_output(self) -> None:
        self.exercise(inbound=True, tracing=False)

    def test_http_context_disabled_preserves_selected_function_telemetry(self) -> None:
        self.exercise(inbound=True, http=False)

    def test_source_exclusion_suppresses_functions_without_disabling_http_context(self) -> None:
        self.exercise(inbound=True, excluded=True)

    def test_native_wsgi_close_keeps_getter_side_effects_and_the_second_lookup_callable(self) -> None:
        source = '''import json,sys,threading,urllib.request
from wsgiref.simple_server import make_server,WSGIRequestHandler
class Body:
    lookups=0
    calls=[]
    def __iter__(self): return iter([b'ok'])
    def __getattribute__(self,name):
        if name=='close':
            Body.lookups+=1
            if sys.argv[1]=='changing':
                which=Body.lookups
                return lambda: Body.calls.append(which)
        return object.__getattribute__(self,name)
    def close(self): Body.calls.append('closed')
class Quiet(WSGIRequestHandler):
    def log_message(self,*arguments): pass
def app(environ,start_response):
    start_response('200 OK',[('Content-Length','2')])
    return Body()
server=make_server('127.0.0.1',0,app,handler_class=Quiet)
worker=threading.Thread(target=server.handle_request)
worker.start()
with urllib.request.urlopen('http://127.0.0.1:'+str(server.server_port)+'/') as response:
    body=response.read()
worker.join(5)
assert not worker.is_alive()
server.server_close()
print(json.dumps({'lookups':Body.lookups,'calls':Body.calls,'body':body.hex()}))
'''
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            entry = root / 'entry.py'
            entry.write_text(source)
            policy = root / 'otelc.toml'
            policy.write_text('''schema_version=3
languages=['python']
[sources]
include=['*.py']
[functions]
include=['*never_selected*']
[metrics]
collection=false
emission=false
[traces]
enabled=false
[context]
http=true
''')
            for mode in ('ordinary', 'changing'):
                with self.subTest(mode=mode):
                    native = subprocess.run([sys.executable, str(entry), mode], cwd=root,
                        capture_output=True, timeout=20)
                    runtime = root / mode
                    observed = subprocess.run([sys.executable, '-B', '-m', 'otelc',
                        '--config', str(policy), 'python', '--root', str(root),
                        '--entry', 'entry.py', '--runtime', str(runtime), '--', mode],
                        cwd=ROOT, capture_output=True, timeout=30)
                    self.assertEqual((native.returncode, native.stderr), (0, b''))
                    self.assertEqual(json.loads(native.stdout), {'lookups': 2,
                        'calls': [2] if mode == 'changing' else ['closed'], 'body': '6f6b'})
                    self.assertEqual((observed.returncode, observed.stdout, observed.stderr),
                                     (native.returncode, native.stdout, native.stderr))
                    report = json.loads((runtime / 'sdk/python-sdk-status.json').read_bytes())
                    self.assertEqual(report['phase'], 'closed')
                    self.assertFalse(any(report['producer']['runtime_losses'].values()))
            self.assertEqual(entry.read_text(), source)

    def exercise(self, *, inbound=False, tracing=True, ratio=1., http=True, excluded=False) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        downstream = Downstream()
        self.addCleanup(downstream.close)
        source = SOURCE.read_bytes()
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            policy = root / 'http.toml'
            text = (ROOT / 'examples/http-context.toml').read_text()
            text = text.replace('http://127.0.0.1:4320', f'http://127.0.0.1:{capture.server.server_port}')
            text = text.replace('http = true', f'http = {str(http).lower()}')
            text = text.replace('enabled = true', f'enabled = {str(tracing).lower()}')
            text = text.replace('sample_ratio = 1.0', f'sample_ratio = {ratio}')
            text = text.replace('exclude = []', 'exclude = ["**"]' if excluded else 'exclude = []')
            text = text.replace('interval_ms = 1000', 'interval_ms = 100000')
            policy.write_text(text)
            outcomes = []
            reports = []
            for plain in (True, False):
                lane = root / ('plain' if plain else 'instrumented')
                lane.mkdir()
                with ExitStack() as stack:
                    def launch(role, argument):
                        runtime = lane / role
                        command = [sys.executable, '-B', '-m', 'otelc', '--config', str(policy),
                            'python', '--root', str(ROOT), '--entry', str(SOURCE), '--runtime', str(runtime),
                            *(['--plain'] if plain else []), '--', role, argument,
                            f'http://127.0.0.1:{downstream.server.server_port}']
                        process = stack.enter_context(subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                        stack.callback(stop, process)
                        return process, runtime

                    server, server_runtime = launch('server', str(lane / 'requests.jsonl'))
                    ready = json.loads(server.stdout.readline())
                    client, client_runtime = launch('client', 'http://127.0.0.1:' + str(ready['port']))
                    client_ready = json.loads(client.stdout.readline())
                    identities = [(server, server_runtime, ready['pid']), (client, client_runtime, client_ready['pid'])]
                    if not plain:
                        sdk_pids = {}
                        for process, runtime, pid in identities:
                            sdk_pids[pid] = control.wait_ready(runtime / 'control.json', pid)['sdk_pid']
                    replies = []
                    for index, (collection, emission) in enumerate(((False, False), (True, False), (False, True), (True, True))):
                        if not plain:
                            for process, runtime, pid in identities:
                                control.request(runtime / 'control.json', 'collection ' + ('on' if collection else 'off'))
                                state = control.request(runtime / 'control.json', 'emission ' + ('on' if emission else 'off'))
                                self.assertEqual((state['pid'], state['collection'], state['emission']), (pid, collection, emission))
                                self.assertEqual(state['sdk_pid'], sdk_pids[pid])
                        replies.extend(batch(client, json.dumps({'path': path})) for path in ('/ok', '/error'))
                        if inbound:
                            for offset, path, sampled in ((1, '/sampled', True), (101, '/unsampled', False), (201, '/relay', True)):
                                request = urllib.request.Request(f'http://127.0.0.1:{ready["port"]}{path}', headers={
                                    'traceparent': f'00-{index + offset:032x}-{index + 1:016x}-0{int(sampled)}',
                                    'tracestate': 'vendor=ordinary', 'baggage': 'session=ordinary', 'X-Business': 'unchanged'})
                                with urllib.request.urlopen(request, timeout=10) as response:
                                    self.assertEqual((response.code, response.read()), (200, b'8'))
                        if not plain:
                            for _, runtime, _ in identities:
                                control.request(runtime / 'control.json', 'flush')
                    for process, runtime, _ in reversed(identities):
                        out, err = process.communicate('quit\n', timeout=20)
                        self.assertEqual((process.returncode, out, err), (0, '', ''))
                        if not plain:
                            report = json.loads((runtime / 'sdk/python-sdk-status.json').read_text())
                            self.assertEqual(report['phase'], 'closed')
                            reports.append(report)
                    outcomes.append(replies)
            self.assertEqual(outcomes[0], [{'status': code, 'body': body, 'unchanged': True}
                for code, body in ((200, '8'), (500, 'bad'))] * 4)
            self.assertEqual(outcomes[1], outcomes[0])
            self.assertEqual(SOURCE.read_bytes(), source)
            self.assertEqual([r['producer']['function_completions'] for r in reports],
                             [0, 0] if excluded else [8, 24 if inbound else 8])
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            clients = {span.span_id: span for span in spans if span.kind == 3}
            servers = [span for span in spans if span.kind == 2]
            expected = (56, 12, 16) if inbound else (32, 8, 8)
            if excluded:
                expected = (28, 12, 16)
            if not http:
                expected = (32, 0, 0)
            if ratio == 0.:
                expected = (24, 4, 8)
            if not tracing:
                expected = (0, 0, 0)
            self.assertEqual((len(spans), len(clients), len(servers)), expected)
            if http and tracing and ratio:
                self.assertEqual(sum(span.parent_span_id in clients and
                    span.trace_id == clients[span.parent_span_id].trace_id for span in servers), 8)
                self.assertEqual(sum(span.status.code == 2 for span in spans), 8)
            if inbound and http and tracing:
                for index in range(4):
                    for offset in (1, 201):
                        incoming = [span for span in servers if span.trace_id == (index + offset).to_bytes(16, 'big')]
                        self.assertEqual(len(incoming), 1)
                        self.assertEqual((incoming[0].parent_span_id, incoming[0].trace_state),
                                         ((index + 1).to_bytes(8, 'big'), 'vendor=ordinary'))
                    self.assertFalse(any(span.trace_id == (index + 101).to_bytes(16, 'big') for span in spans))
                forwarded = downstream.received[-4:]
                self.assertEqual([value[1] for value in forwarded], ['session=ordinary'] * 4)
                for index, (traceparent, _) in enumerate(forwarded):
                    self.assertEqual(traceparent.split('-')[1], f'{index + 201:032x}')
                    self.assertTrue(any(span.kind == 3 and span.span_id.hex() == traceparent.split('-')[2]
                                        and span.trace_state == 'vendor=ordinary' for span in spans))
            for name in ('http.client.request.duration', 'http.server.request.duration'):
                latest = {}
                starts = {}
                for key, point in metric_points(capture, name, 'histogram'):
                    self.assertGreaterEqual(point.count, latest.get(key, 0))
                    self.assertEqual(point.start_time_unix_nano, starts.get(key, point.start_time_unix_nano))
                    latest[key], starts[key] = point.count, point.start_time_unix_nano
                expected_count = (6 if 'client' in name else 10) if inbound else 4
                self.assertEqual(sum(latest.values()), expected_count if http else 0)
