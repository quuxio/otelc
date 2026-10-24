"""Native TLS HTTP/2, including concurrent streams, keeps stock app semantics."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import unittest

from ci import build_go_upstream as tooling
from otelc import control, go_build, prepared_go as prepared
from otelc.config import Policy
from otelc.prepared_python import stop_group
from test_java_context import calls
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class GoHTTP2Tests(unittest.TestCase):
    def test_stock_and_woven_tls_http2_keep_concurrent_streams_and_parentage(self) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='go-http2-', dir=ROOT / 'build') as temporary:
            directory = Path(temporary).resolve()
            original = go_build.inputs(ROOT / 'examples/go-http2')
            data = copy.deepcopy(Policy.load(ROOT / 'examples/http-context.toml').data)
            data['export'].update(endpoint=f'http://127.0.0.1:{capture.server.server_port}', interval_ms=100000)
            policy = Policy.parse(data)
            tools = tooling.build('go', ROOT / 'build/go-upstream')
            build = directory / 'build'
            go_build.build_project(ROOT / 'examples/go-http2', '.', policy, build, tools)
            processes = []

            def line(owner: subprocess.Popen):
                self.assertTrue(select.select([owner.stdout], [], [], 15)[0], 'native HTTP/2 response missing')
                return json.loads(owner.stdout.readline())

            def start(role: str, plain: bool, settings: dict | None = None):
                runtime = directory / (('plain-' if plain else 'woven-') + role)
                arguments = (role,) if settings is None else (role, json.dumps(settings))
                prepared.prepare(policy, runtime, build_directory=build, arguments=arguments, plain=plain)
                owner = subprocess.Popen(prepared.command(runtime), start_new_session=True, text=True,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                processes.append((owner, runtime))
                ready = line(owner)
                if not plain:
                    actual = prepared.read(runtime / 'go-application-start.json')
                    control.wait_ready(runtime / 'control.json', actual['pid'])
                return owner, runtime, ready

            try:
                for plain in (True, False):
                    server, sr, settings = start('server', plain)
                    client, cr, _ = start('client', plain, settings)
                    for command, count in (('single', 1), ('concurrent', 4)):
                        client.stdin.write(command + '\n'); client.stdin.flush()
                        replies = line(client)
                        self.assertEqual(len(replies), count)
                        for reply in replies:
                            self.assertEqual((reply['value'], reply['protocol'], reply['original']),
                                             ('42', 'HTTP/2.0', 'original'))
                            if plain:
                                self.assertEqual(reply['header'], 'original')
                            else:
                                self.assertRegex(reply['header'], r'^00-[0-9a-f]{32}-[0-9a-f]{16}-01$')
                        if not plain:
                            self.assertEqual(len({reply['header'] for reply in replies}), count)
                            if command == 'single':
                                for _, runtime in ((client, cr), (server, sr)):
                                    control.request(runtime / 'control.json', 'flush')
                                    state = control.request(runtime / 'control.json', 'on')
                                    self.assertEqual(state['pid'], prepared.read(runtime / 'go-application-start.json')['pid'])
                                self.assertFalse(capture.metrics, 'startup-off HTTP/2 emitted a metric snapshot')
                    for owner, runtime in ((client, cr), (server, sr)):
                        owner.stdin.write('quit\n'); owner.stdin.flush()
                        code = owner.wait(timeout=15)
                        self.assertEqual((code, owner.stderr.read()), (0, ''))
                        result = prepared.execution(runtime, supervisor_pid=owner.pid, parent_pid=os.getpid(), returncode=code)
                        self.assertEqual(result['failures'], {})
                        if not plain:
                            self.assertEqual(result['sdk_health']['http_spans'],
                                {'observed': True, 'closed': True, 'started': '5', 'ended': '5', 'active': '0'})
            finally:
                for owner, _ in processes:
                    stop_group(owner)
                    for stream in (owner.stdin, owner.stdout, owner.stderr):
                        if stream:
                            stream.close()
            self.assertEqual(go_build.inputs(ROOT / 'examples/go-http2'), original)
        spans = [span for packet in capture.traces for resource in packet.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        clients = {span.span_id for span in spans if span.kind == 3}
        servers = [span for span in spans if span.kind == 2]
        self.assertEqual((len(clients), len(servers), len(spans)), (5, 5, 20))
        self.assertTrue(all(span.parent_span_id in clients for span in servers))
        self.assertEqual(calls(capture), 8)
