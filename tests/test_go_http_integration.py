"""Actual unedited native HTTP services share W3C/task context and live gates."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import unittest
import urllib.request
from typing import Any

from ci import build_go_upstream as tooling
from otelc import control, go_build, prepared_go as prepared
from otelc.config import Policy
from otelc.prepared_python import stop_group
from test_java_context import calls
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class GoHTTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='go-http-', dir=ROOT / 'build')
        cls.base = Path(cls.temporary.name).resolve()
        cls.original = go_build.inputs(ROOT / 'examples/go-http')
        cls.tools = tooling.build('go', ROOT / 'build/go-upstream')
        data = copy.deepcopy(Policy.load(ROOT / 'examples/http-context.toml').data)
        data['context']['tasks'] = True
        cls.policy = Policy.parse(data)
        cls.build = cls.base / 'build'
        go_build.build_project(ROOT / 'examples/go-http', '.', cls.policy, cls.build, cls.tools)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.processes = []
        self.addCleanup(self.cleanup)
        self.sequence = 0

    def cleanup(self) -> None:
        for owner, _ in self.processes:
            stop_group(owner)
            for stream in (owner.stdin, owner.stdout, owner.stderr):
                if stream is not None:
                    stream.close()

    def settings(self, service: str, *, flags: tuple[bool, bool] = (False, False), traces: bool = True) -> Policy:
        data = copy.deepcopy(self.policy.data)
        data['resource']['service_name'] = service
        data['metrics'].update(collection=flags[0], emission=flags[1])
        data['traces']['enabled'] = traces
        data['export'].update(endpoint=f'http://127.0.0.1:{self.capture.server.server_port}', interval_ms=100000)
        return Policy.parse(data)

    def start(self, role: str, policy: Policy, url: str = '', *, plain: bool = False,
              build: Path | None = None) -> tuple[subprocess.Popen, Path, dict]:
        self.sequence += 1
        runtime = self.base / (self._testMethodName + '-' + str(self.sequence))
        prepared.prepare(policy, runtime, build_directory=build or self.build,
                         arguments=(role,) if role == 'server' else (role, url), plain=plain)
        owner = subprocess.Popen(prepared.command(runtime), start_new_session=True, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append((owner, runtime))
        first = self.line(owner)
        actual = prepared.read(runtime / 'go-application-start.json')
        if not plain:
            control.wait_ready(runtime / 'control.json', actual['pid'])
        return owner, runtime, first

    def line(self, owner: subprocess.Popen) -> dict:
        if not select.select([owner.stdout], [], [], 15)[0]:
            self.fail('ordinary Go HTTP app did not respond')
        value = owner.stdout.readline()
        self.assertTrue(value, 'ordinary Go HTTP app exited before its response')
        return json.loads(value)

    def exchange(self, owner: subprocess.Popen, text: str) -> dict:
        owner.stdin.write(text + '\n'); owner.stdin.flush()
        return self.line(owner)

    def finish(self, owner: subprocess.Popen, runtime: Path, *, expected: int = 0) -> dict:
        owner.stdin.write('quit\n'); owner.stdin.flush()
        self.assertEqual(owner.wait(timeout=15), expected, owner.stderr.read())
        result = prepared.execution(runtime, supervisor_pid=owner.pid, parent_pid=os.getpid(), returncode=expected)
        self.assertEqual(result['application_exit_code'], 0)
        return result

    def spans(self, service: str) -> list[Any]:
        return [span for request in self.capture.traces for resource in request.resource_spans
                if any(a.key == 'service.name' and a.value.string_value == service for a in resource.resource.attributes)
                for scope in resource.scope_spans for span in scope.spans]

    def test_two_services_all_startup_and_live_states_keep_context_and_collection(self) -> None:
        for startup in ((False, False), (True, False), (False, True), (True, True)):
            label = ''.join(str(int(v)) for v in startup)
            server_name, client_name = 'go-http-server-' + label, 'go-http-client-' + label
            server, server_runtime, ready = self.start('server', self.settings(server_name, flags=startup))
            client, client_runtime, _ = self.start('client', self.settings(client_name, flags=startup), ready['url'])
            pid = prepared.read(client_runtime / 'go-application-start.json')['pid']
            for flags in ((True, True), (True, False), (False, True), (False, False)):
                for runtime in (server_runtime, client_runtime):
                    control.request(runtime / 'control.json', 'collection ' + ('on' if flags[0] else 'off'))
                    state = control.request(runtime / 'control.json', 'emission ' + ('on' if flags[1] else 'off'))
                    if runtime == client_runtime:
                        self.assertEqual(state['pid'], pid)
                before = len(self.capture.metrics)
                answer = self.exchange(client, 'normal')
                self.assertFalse(answer['error'])
                self.assertEqual(answer['formats'], 0)
                self.assertEqual(answer['result']['value'], '42')
                self.assertEqual(answer['result']['original_header'], 'unchanged-original-header')
                self.assertRegex(answer['result']['wire_context'], r'^00-[0-9a-f]{32}-[0-9a-f]{16}-01$')
                for runtime in (server_runtime, client_runtime):
                    control.request(runtime / 'control.json', 'flush')
                self.assertEqual(len(self.capture.metrics), before + (2 if all(flags) else 0))
            for runtime in (server_runtime, client_runtime):
                control.request(runtime / 'control.json', 'on'); control.request(runtime / 'control.json', 'flush')
            for owner, runtime in ((client, client_runtime), (server, server_runtime)):
                final = self.finish(owner, runtime)
                self.assertEqual(final['failures'], {})
                self.assertEqual(final['sdk_health']['http_spans'],
                    {'observed': True, 'closed': True, 'started': '4', 'ended': '4', 'active': '0'})
            client_spans, server_spans = self.spans(client_name), self.spans(server_name)
            self.assertEqual((len(client_spans), len(server_spans)), (8, 8))
            all_spans = {span.span_id: span for span in [*client_spans, *server_spans]}
            for span in client_spans:
                if span.kind == 3:
                    self.assertEqual(all_spans[span.parent_span_id].name, 'io.quux.otelc.examples/gohttp.workloadRequest')
            for span in server_spans:
                parent = all_spans[span.parent_span_id]
                self.assertEqual(span.trace_id, parent.trace_id)
                self.assertEqual(parent.kind, 3 if span.kind == 2 else 2)
        self.assertEqual(calls(self.capture), 16)
        self.assertTrue(any(m.name.startswith('http.') for r in self.capture.metrics
                            for resource in r.resource_metrics for scope in resource.scope_metrics for m in scope.metrics))
        self.assertEqual(go_build.inputs(ROOT / 'examples/go-http'), self.original)

    def test_native_errors_panic_and_cancellation_preserve_application_results(self) -> None:
        results = []
        for plain in (True, False):
            server, sr, ready = self.start('server', self.settings('go-http-errors-server-' + str(plain)), plain=plain)
            client, cr, _ = self.start('client', self.settings('go-http-errors-client-' + str(plain)), ready['url'], plain=plain)
            answers = [self.exchange(client, text) for text in ('error', 'cancel', 'panic')]
            self.assertTrue(answers[0]['error']); self.assertTrue(answers[0]['result']['error_same'])
            self.assertTrue(answers[1]['error']); self.assertEqual(answers[2], {'panic': True, 'formats': 0})
            self.assertTrue(all(answer['formats'] == 0 for answer in answers))
            results.append(answers)
            self.assertEqual(self.finish(client, cr)['failures'], {})
            self.assertEqual(self.finish(server, sr)['failures'], {})
        self.assertEqual(results[0], results[1])

    def test_unclosed_response_body_is_visible_and_cannot_pass_final_health(self) -> None:
        server, sr, ready = self.start('server', self.settings('go-http-leak-server'))
        client, cr, _ = self.start('client', self.settings('go-http-leak-client'), ready['url'])
        answer = self.exchange(client, 'leak')
        self.assertEqual(answer['result']['value'], 'retained')
        final = self.finish(client, cr, expected=1)
        self.assertEqual(final['sdk_health']['http_spans'],
                         {'observed': True, 'closed': True, 'started': '1', 'ended': '0', 'active': '1'})
        self.assertTrue(final['failures'])
        self.assertEqual(self.finish(server, sr)['failures'], {})

    def test_returned_request_context_keeps_each_native_http_operation_visible(self) -> None:
        server, sr, ready = self.start('server', self.settings('go-http-reuse-server'))
        client, cr, _ = self.start('client', self.settings('go-http-reuse-client'), ready['url'])
        answer = self.exchange(client, 'reuse')
        self.assertFalse(answer['error'])
        self.assertEqual(answer['result']['value'], '42')
        self.assertEqual(answer['result']['reused_value'], '42')
        self.finish(client, cr); self.finish(server, sr)
        for runtime in (cr, sr):
            self.assertEqual(prepared.read(runtime / 'health.json')['http_spans'],
                             {'observed': True, 'closed': True, 'started': '2', 'ended': '2', 'active': '0'})
        clients = [span for span in self.spans('go-http-reuse-client') if span.kind == 3]
        servers = [span for span in self.spans('go-http-reuse-server') if span.kind == 2]
        self.assertEqual((len(clients), len(servers)), (2, 2))
        self.assertEqual({(span.trace_id, span.parent_span_id) for span in servers},
                         {(span.trace_id, span.span_id) for span in clients})

    def test_tracing_off_preserves_context_with_no_recorded_spans_or_invented_counters(self) -> None:
        server, sr, ready = self.start('server', self.settings('go-http-unrecorded-server', traces=False))
        client, cr, _ = self.start('client', self.settings('go-http-unrecorded-client', traces=False), ready['url'])
        answer = self.exchange(client, 'normal')
        self.assertRegex(answer['result']['wire_context'], r'^00-[0-9a-f]{32}-[0-9a-f]{16}-00$')
        for owner, runtime in ((client, cr), (server, sr)):
            final = self.finish(owner, runtime)
            self.assertEqual(final['failures'], {})
            self.assertEqual(final['sdk_health']['http_spans'], {'observed': False})
        self.assertFalse(self.capture.traces)

    def test_http_server_function_context_reaches_joined_goroutine(self) -> None:
        server, sr, ready = self.start('server', self.settings('go-http-child-server'))
        client, cr, _ = self.start('client', self.settings('go-http-child-client'), ready['url'])
        self.assertEqual(self.exchange(client, 'child')['result']['value'], '42')
        self.finish(client, cr); self.finish(server, sr)
        spans = self.spans('go-http-child-server')
        child = next(span for span in spans if span.name.endswith('.workloadChild'))
        parent = next(span for span in spans if span.name.endswith('.workloadHandle'))
        self.assertEqual((child.trace_id, child.parent_span_id), (parent.trace_id, parent.span_id))

    def test_disabled_task_inheritance_keeps_http_context_without_child_parentage(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['context']['tasks'] = False
        policy = Policy.parse(data)
        build = self.base / 'build-without-tasks'
        go_build.build_project(ROOT / 'examples/go-http', '.', policy, build, self.tools)

        def settings(service: str) -> Policy:
            data = copy.deepcopy(self.settings(service).data)
            data['context']['tasks'] = False
            return Policy.parse(data)

        server, sr, ready = self.start('server', settings('go-http-no-task-server'), build=build)
        client, cr, _ = self.start('client', settings('go-http-no-task-client'), ready['url'], build=build)
        self.assertEqual(self.exchange(client, 'child')['result']['value'], '42')
        self.finish(client, cr); self.finish(server, sr)
        server_spans = self.spans('go-http-no-task-server')
        child = next(span for span in server_spans if span.name.endswith('.workloadChild'))
        parent = next(span for span in server_spans if span.name.endswith('.workloadHandle'))
        self.assertFalse(child.parent_span_id)
        self.assertNotEqual(child.trace_id, parent.trace_id)
        http = next(span for span in server_spans if span.kind == 2)
        client_http = next(span for span in self.spans('go-http-no-task-client') if span.kind == 3)
        self.assertEqual((http.trace_id, http.parent_span_id), (client_http.trace_id, client_http.span_id))

    def test_external_w3c_remote_parent_is_retained_without_application_telemetry(self) -> None:
        server, runtime, ready = self.start('server', self.settings('go-http-external'))
        remote_trace, remote_parent = '1' * 32, '2' * 16
        request = urllib.request.Request(ready['url'], headers={'traceparent': '00-' + remote_trace + '-' + remote_parent + '-01'})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.read(), b'42')
        self.finish(server, runtime)
        spans = self.spans('go-http-external')
        http = next(span for span in spans if span.kind == 2)
        function = next(span for span in spans if span.kind == 1)
        self.assertEqual((http.trace_id.hex(), http.parent_span_id.hex()), (remote_trace, remote_parent))
        self.assertEqual((function.trace_id, function.parent_span_id), (http.trace_id, http.span_id))

    def test_eager_http_bootstrap_precedes_unselected_application_initialisers(self) -> None:
        root = self.base / 'initialising-app'
        root.mkdir()
        (root / 'go.mod').write_text('module example.com/httpinit\n\ngo 1.26.0\n')
        (root / 'main.go').write_text('''package main
import("fmt";"io";"net/http";"net/http/httptest")
var owned,official,probe,bootstrap=1,2,3,4
var result=func()string{
 s:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){fmt.Fprint(w,32+owned+official+probe+bootstrap)}));defer s.Close()
 r,err:=http.Get(s.URL);if err!=nil{panic(err)};defer r.Body.Close();b,_:=io.ReadAll(r.Body);return string(b)
}()
func main(){fmt.Println(result)}
''')
        data = copy.deepcopy(self.settings('go-http-initialisers').data)
        data['functions']['include'] = ['does-not-match']
        policy = Policy.parse(data)
        build = self.base / 'initialiser-build'
        go_build.build_project(root, '.', policy, build, self.tools)
        runtime = self.base / 'initialiser-runtime'
        prepared.prepare(policy, runtime, build_directory=build)
        owner = subprocess.Popen(prepared.command(runtime), start_new_session=True, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append((owner, runtime))
        out, err = owner.communicate(timeout=15)
        self.assertEqual((owner.returncode, out, err), (0, b'42\n', b''))
        final = prepared.execution(runtime, supervisor_pid=owner.pid, parent_pid=os.getpid(), returncode=0)
        self.assertEqual(final['failures'], {})
        self.assertEqual(final['sdk_health']['probe_accounting']['function_started'], '0')
        self.assertEqual(final['sdk_health']['http_spans']['ended'], '2')
        self.assertEqual(len(self.spans('go-http-initialisers')), 2)


if __name__ == '__main__':
    unittest.main()
