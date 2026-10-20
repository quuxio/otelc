"""Real standard-library request boundaries with original application handlers."""
import contextlib
import http.client
import http.server
import sys
import threading
import time
import unittest
from unittest.mock import patch

from opentelemetry.trace import SpanKind

from test_python_spans import PythonSpanHarness, trace_plan


PARENT = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-03'
SOURCE = '''
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        if self.path == '/wait':
            entered.set()
            if not released.wait(5):
                raise TimeoutError('test gate timed out')
        if self.path == '/caught':
            try:
                raise TimeoutError('handled by application')
            except TimeoutError:
                pass
        if self.path == '/timeout':
            raise TimeoutError('original timeout')
        if self.path == '/escape':
            raise original_error
        body = b'original response' if self.path == '/shallow' else self.reply()
        self.send_response(503 if self.path == '/unavailable' else 200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply(self):
        return b'original response'

    def log_message(self, *_args):
        pass
'''


@contextlib.contextmanager
def serve(handler):
    errors = []

    class Server(http.server.ThreadingHTTPServer):
        def handle_error(self, *_args):
            errors.append(sys.exc_info()[1])

    server = Server(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_port, errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        if thread.is_alive():
            raise AssertionError('server did not stop')


class PythonHTTPContextTests(PythonSpanHarness, unittest.TestCase):
    def monitored_server(self, policy=None):
        policy = policy or trace_plan()
        policy['propagation'] = {'http': True, 'tasks': policy.get('propagation', {}).get('tasks', False)}
        policy['function_matchers']['exclude'] = [r'(?-u).*\.log_message$']
        source = 'from http.server import BaseHTTPRequestHandler\n' + SOURCE
        runtime, metrics, capture, namespace, monitor = self.monitored(source, policy)
        namespace['original_error'] = ValueError('original handler exception')
        return runtime, capture, namespace, monitor

    def test_keepalive_requests_share_global_trace_but_own_independent_local_trees(self):
        runtime, capture, app, monitor = self.monitored_server()
        with serve(app['Handler']) as (port, errors):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                for parent in (PARENT, PARENT.replace('00f067aa0ba902b7', '00f067aa0ba902b8')):
                    connection.request('GET', '/', headers={'TraceParent': parent, 'TraceState': 'first=1,second=2'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b'original response')
            finally:
                connection.close()
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        servers = [span for span in spans if span.kind == 2]
        self.assertEqual(len(servers), 2)
        self.assertEqual([span.parent_span_id.hex() for span in servers], ['00f067aa0ba902b7', '00f067aa0ba902b8'])
        self.assertEqual(len({span.trace_id for span in spans}), 1)
        self.assertEqual(report['traces']['completed_trees'], 2)
        self.assertFalse(report['traces']['losses'])
        self.assertFalse(errors)
        for server in servers:
            children = [span for span in spans if span.parent_span_id == server.span_id]
            self.assertEqual([span.name for span in children], ['trace_fixture.Handler.do_GET'])
            self.assertEqual(server.trace_state, 'first=1,second=2')

    def test_unsampled_inbound_context_suppresses_selected_functions_without_losing_it(self):
        runtime, capture, app, monitor = self.monitored_server()
        with serve(app['Handler']) as (port, _):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/', headers={'traceparent': PARENT[:-2] + '02'})
            self.assertEqual(connection.getresponse().read(), b'original response')
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertFalse(capture.spans())
        self.assertEqual(report['traces']['sampled_out_roots'], 1)
        self.assertGreater(report['function_calls'], 0)
        self.assertFalse(report['traces']['losses'])

    def test_original_escaping_error_and_server_error_status_are_preserved(self):
        runtime, capture, app, monitor = self.monitored_server()
        with serve(app['Handler']) as (port, errors):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/unavailable')
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertEqual(response.read(), b'original response')
            connection.close()
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/escape')
            with self.assertRaises(http.client.RemoteDisconnected):
                connection.getresponse()
            connection.close()
        self.finish(runtime, monitor)
        self.assertEqual(len(errors), 1)
        self.assertIs(errors[0], app['original_error'])
        servers = [span for span in capture.spans() if span.kind == 2]
        self.assertEqual([span.status.code for span in servers], [2, 2])
        self.assertNotIn('original handler exception', str(capture.requests))
        self.assertTrue(any(attribute.key == 'http.response.status_code' and attribute.value.int_value == 503
                            for attribute in servers[0].attributes))

    def test_duplicate_parent_fields_start_a_local_root_and_invalid_state_is_independent(self):
        runtime, capture, app, monitor = self.monitored_server()
        with serve(app['Handler']) as (port, _):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.putrequest('GET', '/')
            connection.putheader('traceparent', PARENT)
            connection.putheader('TraceParent', PARENT)
            connection.endheaders()
            self.assertEqual(connection.getresponse().read(), b'original response')
            connection.request('GET', '/', headers={'traceparent': PARENT, 'tracestate': 'a=1,a=2'})
            self.assertEqual(connection.getresponse().read(), b'original response')
            connection.close()
        self.finish(runtime, monitor)
        servers = [span for span in capture.spans() if span.kind == 2]
        self.assertFalse(servers[0].parent_span_id)
        self.assertEqual(servers[1].parent_span_id.hex(), '00f067aa0ba902b7')
        self.assertFalse(servers[1].trace_state)

    def test_scope_and_hooks_are_restored_after_shutdown(self):
        before = http.server.BaseHTTPRequestHandler.handle_one_request
        runtime, _, _, monitor = self.monitored_server()
        context = monitor.http
        self.assertIsNotNone(context)
        self.assertIsNone(context.current())
        monitor.close()
        self.assertIs(http.server.BaseHTTPRequestHandler.handle_one_request, before)
        self.assertIsNone(runtime.http)
        self.assertFalse(context.active_count)
        runtime.close()

    def test_timeout_caught_by_the_standard_server_is_an_error_without_an_invented_status(self):
        runtime, capture, app, monitor = self.monitored_server()
        with serve(app['Handler']) as (port, errors):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/timeout')
            with self.assertRaises(http.client.RemoteDisconnected):
                connection.getresponse()
            connection.close()
        self.finish(runtime, monitor)
        servers = [span for span in capture.spans() if span.kind == 2]
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].status.code, 2)
        self.assertFalse(any(attribute.key == 'http.response.status_code' for attribute in servers[0].attributes))
        self.assertFalse(errors)
        self.assertNotIn('original timeout', str(capture.requests))

    def test_application_caught_timeout_does_not_mark_the_http_span_as_failed(self):
        runtime, capture, app, monitor = self.monitored_server()
        with serve(app['Handler']) as (port, _):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/caught')
            self.assertEqual(connection.getresponse().read(), b'original response')
            connection.close()
        self.finish(runtime, monitor)
        servers = [span for span in capture.spans() if span.kind == 2]
        self.assertEqual([span.status.code for span in servers], [0])

    def test_opaque_headers_do_not_call_extra_getters_or_promote_function_roots(self):
        class OpaqueHeaders(http.client.HTTPMessage):
            def get_all(self, *_args, **_kwargs):
                raise AssertionError('instrumentation dispatched an application getter')

        runtime, capture, app, monitor = self.monitored_server()
        app['Handler'].MessageClass = OpaqueHeaders
        with serve(app['Handler']) as (port, _):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/')
            self.assertEqual(connection.getresponse().read(), b'original response')
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertFalse(capture.spans())
        self.assertEqual(report['traces']['losses'], {'http_unsupported_headers': 1})

    def test_http_and_function_admission_share_the_active_call_budget_and_recover(self):
        policy = trace_plan()
        policy['runtime']['max_active_calls'] = 2
        runtime, capture, app, monitor = self.monitored_server(policy)
        with serve(app['Handler']) as (port, _):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            for path in ('/', '/shallow'):
                connection.request('GET', path)
                self.assertEqual(connection.getresponse().read(), b'original response')
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual([span.name for span in capture.spans()], ['HTTP server', 'trace_fixture.Handler.do_GET'])
        self.assertEqual(report['traces']['losses'], {'active_call_capacity': 1})
        self.assertFalse(report['traces']['active_trees'])

    def test_stopping_hooks_censors_active_requests_instead_of_exporting_completion(self):
        runtime, capture, app, monitor = self.monitored_server()
        app.update(entered=threading.Event(), released=threading.Event())
        with serve(app['Handler']) as (port, _):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                connection.request('GET', '/wait')
                self.assertTrue(app['entered'].wait(3))
                self.assertEqual(monitor.http.active_count, 1)
                monitor.http.close()
                app['released'].set()
                self.assertEqual(connection.getresponse().read(), b'original response')
            finally:
                app['released'].set()
                connection.close()
        report = self.finish(runtime, monitor)
        self.assertFalse(capture.spans())
        self.assertEqual(report['traces']['losses'], {'http_incomplete': 1})

    def test_an_application_replacement_is_not_overwritten_during_shutdown(self):
        original = http.server.BaseHTTPRequestHandler.send_response_only
        runtime, _, _, monitor = self.monitored_server()

        def replacement(*args, **kwargs):
            return original(*args, **kwargs)

        try:
            http.server.BaseHTTPRequestHandler.send_response_only = replacement
            monitor.close()
            self.assertIs(http.server.BaseHTTPRequestHandler.send_response_only, replacement)
        finally:
            http.server.BaseHTTPRequestHandler.send_response_only = original
        runtime.close()

    def test_second_installation_is_refused_without_leaking_tools_or_replacing_the_first(self):
        _, _, _, monitor = self.monitored_server()
        wrapper = http.server.BaseHTTPRequestHandler.handle_one_request
        tools = [sys.monitoring.get_tool(number) for number in range(6)]
        with self.assertRaisesRegex(ValueError, 'already installed'):
            self.monitored_server()
        self.assertEqual([sys.monitoring.get_tool(number) for number in range(6)], tools)
        self.assertIs(http.server.BaseHTTPRequestHandler.handle_one_request, wrapper)
        self.assertFalse(monitor.http.closed)

    def test_sdk_admission_failure_preserves_response_and_suppresses_accidental_function_roots(self):
        runtime, capture, app, monitor = self.monitored_server()
        original = runtime.traces.begin

        def begin(*args, **kwargs):
            if kwargs.get('kind') == SpanKind.SERVER:
                raise RuntimeError('private SDK diagnostic')
            return original(*args, **kwargs)

        with patch.object(runtime.traces, 'begin', side_effect=begin):
            with serve(app['Handler']) as (port, _):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                connection.request('GET', '/')
                self.assertEqual(connection.getresponse().read(), b'original response')
                connection.close()
        report = self.finish(runtime, monitor)
        self.assertFalse(capture.spans())
        self.assertEqual(report['traces']['losses'], {'http_hook': 1})
        self.assertNotIn('private SDK diagnostic', str(report))

    def test_request_scopes_coexist_with_task_context_and_restore_hooks(self):
        policy = trace_plan()
        policy['propagation'] = {'tasks': True}
        runtime, capture, app, monitor = self.monitored_server(policy)
        context = monitor.context
        self.assertIsNone(context.current.get())
        with serve(app['Handler']) as (port, _):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            for parent in (PARENT, PARENT.replace('00f067aa0ba902b7', '00f067aa0ba902b8')):
                connection.request('GET', '/', headers={'traceparent': parent})
                self.assertEqual(connection.getresponse().read(), b'original response')
            connection.close()
        self.assertIsNone(context.current.get())
        report = self.finish(runtime, monitor)
        self.assertEqual([span.parent_span_id.hex() for span in capture.spans() if span.kind == 2],
                         ['00f067aa0ba902b7', '00f067aa0ba902b8'])
        self.assertFalse(report['traces']['losses'])

    def test_header_observation_does_not_add_attribute_or_dictionary_getter_calls(self):
        runtime, capture, app, monitor = self.monitored_server()
        reads = []

        def access(instance, name):
            if name == 'headers':
                reads.append(name)
            return object.__getattribute__(instance, name)

        def hidden_dictionary(_instance):
            raise AssertionError('instrumentation dispatched an application dictionary getter')

        app['Handler'] = type('Guarded', (app['Handler'],), {
            '__getattribute__': access, '__dict__': property(hidden_dictionary)})
        monitor.close()
        counts = []
        for enabled in (False, True):
            if enabled:
                monitor.install()
            before = len(reads)
            with serve(app['Handler']) as (port, _):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                connection.request('GET', '/', headers={'traceparent': PARENT})
                self.assertEqual(connection.getresponse().read(), b'original response')
                connection.close()
            counts.append(len(reads) - before)
        report = self.finish(runtime, monitor)
        self.assertGreater(counts[0], 0)
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(len([span for span in capture.spans() if span.kind == 2]), 1)
        self.assertFalse(report['traces']['losses'])

    def test_sdk_failure_during_hook_shutdown_does_not_replace_application_shutdown(self):
        runtime, capture, app, monitor = self.monitored_server()
        app.update(entered=threading.Event(), released=threading.Event())
        with serve(app['Handler']) as (port, _):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                connection.request('GET', '/wait')
                self.assertTrue(app['entered'].wait(3))
                with patch.object(runtime.traces, 'finish', side_effect=RuntimeError('private SDK fault')):
                    monitor.close()
                self.assertIsNone(runtime.http)
                app['released'].set()
                self.assertEqual(connection.getresponse().read(), b'original response')
            finally:
                app['released'].set()
                connection.close()
        report = runtime.close()
        self.assertFalse(capture.spans())
        self.assertEqual(report['traces']['losses'], {'http_incomplete': 1, 'http_hook': 1})
