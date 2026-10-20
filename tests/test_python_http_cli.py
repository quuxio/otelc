"""Exercise public policies and two unchanged apps through the actual CLI."""
import hashlib
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_python_spans import ExportTraceServiceRequest, ROOT, receiver


class PythonHTTPCLITests(unittest.TestCase):
    def run_pair(self, folder: Path, environment: dict[str, str],
                 instrumented: bool, path: str, sampled: bool = True) -> dict[str, object]:
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = str(reservation.getsockname()[1])
        commands = {}
        reports = {}
        for role in ('server', 'client'):
            source = ROOT / f'examples/apps/python_http_{role}.py'
            if instrumented:
                config = ROOT / f'examples/python-http-{role}.toml'
                if role == 'client' and not sampled:
                    copied = folder / 'unsampled-client.toml'
                    copied.write_text(config.read_text().replace('root_sample_ratio = 1.0',
                                                                 'root_sample_ratio = 0.0'))
                    config = copied
                commands[role] = [str(ROOT / 'target/debug/quux-otelc'),
                                  '--config', str(config),
                                  'python', str(source)]
            else:
                commands[role] = [sys.executable, str(source)]
            commands[role] += ['--port', port]
            reports[role] = folder / f'{role}.json'
            reports[role].unlink(missing_ok=True)
        server_environment = dict(environment, OTELC_REPORT_PATH=str(reports['server']))
        server = subprocess.Popen(commands['server'], cwd=ROOT, env=server_environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(select.select([server.stdout], [], [], 10)[0],
                            'HTTP server did not become ready')
            ready = server.stdout.readline()
            self.assertEqual(ready, b'ready\n')
            client = subprocess.run(commands['client'] + ['--path', path], cwd=ROOT,
                                    env=dict(environment, OTELC_REPORT_PATH=str(reports['client'])),
                                    capture_output=True, timeout=20)
            stdout, stderr = server.communicate(timeout=20)
            channels = {'client': (client.returncode, client.stdout, client.stderr),
                        'server': (server.returncode, ready + stdout, stderr)}
            for role, (status, _, errors) in channels.items():
                self.assertEqual(status, 0, f'{role}: {errors!r}')
            if instrumented:
                for role, report in reports.items():
                    value = json.loads(report.read_text())
                    self.assertGreater(value['pid'], 0)
                    self.assertTrue(value['export_finished'])
                    self.assertFalse(any(value['losses'].values()))
                    self.assertFalse(value['export_loss'])
                    self.assertFalse(value['traces']['losses'])
                    self.assertFalse(value['traces']['active_trees'])
                    self.assertFalse(value['traces']['queued_trees'])
                    self.assertEqual(value['function_calls'], 1 if role == 'client' else 2)
            return channels
        finally:
            if server.poll() is None:
                server.kill()
            server.communicate(timeout=5)

    def test_common_policy_joins_two_processes_without_changing_results_or_sources(self):
        if not (ROOT / 'target/debug/quux-otelc').exists():
            self.skipTest('build CLI before running Python product tests')
        files = [ROOT / f'examples/{prefix}{role}{suffix}'
                 for role in ('client', 'server')
                 for prefix, suffix in (('apps/python_http_', '.py'), ('python-http-', '.toml'))]
        originals = {file: hashlib.sha256(file.read_bytes()).hexdigest() for file in files}
        with receiver() as (endpoint, requests), tempfile.TemporaryDirectory() as folder:
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('OTEL_', 'OTELC_'))}
            environment.update(OTELC_PYTHON=sys.executable, OTELC_ADAPTER_ROOT=str(ROOT / 'adapters'),
                               OTEL_EXPORTER_OTLP_ENDPOINT=endpoint)
            doctor = subprocess.run([str(ROOT / 'target/debug/quux-otelc'), '--config',
                                     str(ROOT / 'examples/python-http-client.toml'),
                                     '--language', 'python', 'doctor'], cwd=ROOT, env=environment,
                                    capture_output=True, timeout=20)
            self.assertEqual(doctor.returncode, 0, doctor.stderr.decode())
            self.assertIn(b'HTTPConnection/HTTPSConnection/urllib', doctor.stdout)
            for path, sampled in (('/', True), ('/error', True), ('/', False)):
                with self.subTest(path=path, sampled=sampled):
                    requests.clear()
                    plain = self.run_pair(Path(folder), environment, False, path)
                    actual = self.run_pair(Path(folder), environment, True, path, sampled)
                    self.assertEqual(actual, plain)
                    spans = {}
                    services = set()
                    for request_path, _, body in requests:
                        if request_path != '/v1/traces':
                            self.assertEqual(request_path, '/v1/metrics')
                            continue
                        for resource in ExportTraceServiceRequest.FromString(body).resource_spans:
                            services.update(a.value.string_value for a in resource.resource.attributes
                                            if a.key == 'service.name')
                            for scope in resource.scope_spans:
                                self.assertEqual(scope.scope.name, 'quux.otelc')
                                for span in scope.spans:
                                    self.assertNotIn(span.name, spans)
                                    spans[span.name] = span
                    if not sampled:
                        # The server still has ratio 1.0: a sampled server tree
                        # would expose loss of the client's unsampled carrier.
                        self.assertFalse(spans)
                        self.assertFalse(services)
                        continue
                    self.assertEqual(services, {'otelc-python-http-client', 'otelc-python-http-server'})
                    names = ['examples.apps.python_http_client.request_order', 'HTTP client',
                             'HTTP server', 'examples.apps.python_http_server.Handler.do_GET',
                             'examples.apps.python_http_server.process_order']
                    self.assertEqual(set(spans), set(names))
                    ordered = [spans[name] for name in names]
                    self.assertEqual(len({span.trace_id for span in ordered}), 1)
                    self.assertTrue(any(ordered[0].trace_id))
                    self.assertEqual(len({span.span_id for span in ordered}), 5)
                    self.assertFalse(ordered[0].parent_span_id)
                    for parent, child in zip(ordered, ordered[1:]):
                        self.assertEqual(child.parent_span_id, parent.span_id)
                    self.assertEqual([span.kind for span in ordered], [1, 3, 2, 1, 1])
                    self.assertEqual(sum(span.status.code == 2 for span in ordered),
                                     3 if path == '/error' else 0)
        self.assertEqual({file: hashlib.sha256(file.read_bytes()).hexdigest() for file in files}, originals)
