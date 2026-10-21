"""Exercise hooked clients against an ordinary server in a separate interpreter."""
import contextlib
import gc
import http.client
import json
import os
import select
import ssl
import subprocess
import sys
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from test_python_spans import PythonSpanHarness, trace_plan
import test_python_http_context as server_tests


SERVER = r'''
import http.server,json,ssl,sys
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_GET(self):
        body = b'line one\nline two\n'
        if self.path == '/capture':
            body = json.dumps({'traceparent':self.headers.get_all('traceparent',[]),
                               'tracestate':self.headers.get_all('tracestate',[])}).encode()
        self.send_response(503 if self.path == '/error' else 204 if self.path == '/empty' else 200)
        if self.path == '/chunked':
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.wfile.write(b'3\r\nabc\r\n3\r\ndef\r\n0\r\n\r\n')
            return
        if self.path == '/empty':
            body = b''
        self.send_header('Content-Length', '100' if self.path == '/short' else str(len(body)))
        if self.path in ('/short', '/close'):
            self.send_header('Connection','close')
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*args): pass
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Length','18')
        self.end_headers()
server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
if len(sys.argv) == 3:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(sys.argv[1],sys.argv[2])
    server.socket = context.wrap_socket(server.socket,server_side=True)
print(server.server_port,flush=True)
server.serve_forever()
'''


@contextlib.contextmanager
def plain_server(tls=()):
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('OTEL_', 'OTELC_'))}
    child = subprocess.Popen([sys.executable, '-u', '-c', SERVER, *map(str, tls)], env=environment,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if not select.select([child.stdout], [], [], 5)[0]:
            raise AssertionError('plain HTTP server did not become ready')
        line = child.stdout.readline()
        if not line.strip().isdigit():
            raise AssertionError('plain HTTP server did not publish its port')
        yield int(line)
    finally:
        child.terminate()
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)


@contextlib.contextmanager
def certificate():
    with tempfile.TemporaryDirectory(prefix='otelc-http-tls-') as folder:
        key, cert = Path(folder) / 'key.pem', Path(folder) / 'cert.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
                        '-keyout', str(key), '-out', str(cert)], check=True, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        yield cert, key


class PythonHTTPClientTests(PythonSpanHarness, unittest.TestCase):
    def client(self, policy=None):
        policy = policy or trace_plan()
        policy['propagation'] = {'http': True, 'tasks': False}
        runtime, _, capture, app, monitor = self.monitored('def invoke(action):\n    return action()\n', policy)
        return runtime, capture, app, monitor

    def test_request_has_a_selected_parent_and_response_eof_completes_the_tree(self):
        runtime, capture, app, monitor = self.client()
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            response = app['invoke'](lambda: (connection.request('GET', '/capture'), connection.getresponse())[1])
            self.assertEqual(runtime.traces.report()['active_trees'], 1)
            self.assertEqual(monitor.http.clients.active_count, 1)
            value = json.loads(response.read())
            self.assertEqual(len(value['traceparent']), 1)
            self.assertFalse(monitor.http.clients.active_count)
            connection.close()
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        self.assertEqual([span.name for span in spans], ['trace_fixture.invoke', 'HTTP client'])
        self.assertEqual(spans[1].kind, 3)
        self.assertEqual(spans[1].parent_span_id, spans[0].span_id)
        wire = value['traceparent'][0].split('-')
        self.assertEqual(wire[1], spans[1].trace_id.hex())
        self.assertEqual(wire[2], spans[1].span_id.hex())
        self.assertFalse(report['traces']['losses'])

    def test_partial_reads_and_explicit_close_do_not_end_at_response_headers(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/')
            response = connection.getresponse()
            self.assertEqual(response.read(1), b'l')
            self.assertEqual(monitor.http.clients.active_count, 1)
            self.assertFalse(runtime.traces.report()['completed_trees'])
            response.close()
            self.assertEqual(monitor.http.clients.active_count, 0)
            connection.close()
        self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 1)

    def test_connection_close_during_getresponse_does_not_censor_a_valid_body(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/close')
            response = connection.getresponse()
            self.assertIsNone(connection.sock)
            self.assertEqual(monitor.http.clients.active_count, 1)
            self.assertEqual(response.read(), b'line one\nline two\n')
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 1)
        self.assertFalse(report['traces']['losses'])

    def test_failed_unbounded_read_is_an_error_even_when_close_happens_before_the_raise(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/short')
            response = connection.getresponse()
            with self.assertRaises(http.client.IncompleteRead) as error:
                response.read()
            self.assertEqual(error.exception.partial, b'line one\nline two\n')
            self.assertFalse(monitor.http.clients.active_count)
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual([span.status.code for span in capture.spans()], [2])
        self.assertFalse(report['traces']['losses'])

    def test_zero_length_and_error_status_and_connection_reuse(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            for path, status, body in (('/empty', 204, b''), ('/error', 503, b'line one\nline two\n')):
                connection.request('GET', path)
                response = connection.getresponse()
                self.assertEqual(response.status, status)
                if not body:
                    self.assertFalse(monitor.http.clients.active_count)
                self.assertEqual(response.read(), body)
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual([span.status.code for span in capture.spans()], [0, 2])
        self.assertFalse(report['traces']['losses'])

    def test_chunked_and_incremental_read_variants_keep_original_bytes(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            for method in ('read', 'readinto', 'read1', 'readline', 'readlines', 'iterate'):
                with self.subTest(method=method):
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                    connection.request('GET', '/chunked')
                    response = connection.getresponse()
                    if method == 'readinto':
                        data, buffer = bytearray(), bytearray(2)
                        while count := response.readinto(buffer):
                            data.extend(buffer[:count])
                        value = bytes(data)
                    elif method == 'read1':
                        data = bytearray()
                        while chunk := response.read1(2):
                            data.extend(chunk)
                        value = bytes(data)
                    elif method in ('readlines', 'iterate'):
                        value = b''.join(response.readlines() if method == 'readlines' else response)
                    else:
                        value = getattr(response, method)()
                    self.assertEqual(value, b'abcdef')
                    self.assertFalse(monitor.http.clients.active_count)
                    connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 6)
        self.assertFalse(report['traces']['losses'])

    def test_explicit_application_context_is_preserved_and_linked(self):
        runtime, capture, _, monitor = self.client()
        parent = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-03'
        headers = {'TraceParent': parent, 'TraceState': 'a=1,b=2'}
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/capture', headers=headers)
            received = json.loads(connection.getresponse().read())
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual(received, {'traceparent': [parent], 'tracestate': ['a=1,b=2']})
        self.assertEqual(headers, {'TraceParent': parent, 'TraceState': 'a=1,b=2'})
        span = capture.spans()[0]
        self.assertEqual(len(span.links), 1)
        self.assertEqual(span.links[0].span_id.hex(), '00f067aa0ba902b7')
        self.assertEqual(span.links[0].trace_state, 'a=1,b=2')
        self.assertFalse(report['traces']['losses'])

    def test_objects_are_not_retained_after_response_and_connection_disappear(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/close')
            response = connection.getresponse()
            connection_ref, response_ref = weakref.ref(connection), weakref.ref(response)
            del connection
            gc.collect()
            self.assertIsNone(connection_ref())
            self.assertEqual(monitor.http.clients.active_count, 1)
            self.assertEqual(response.read(), b'line one\nline two\n')
            del response
            gc.collect()
            self.assertIsNone(response_ref())
            self.assertFalse(monitor.http.clients.active_count)
        report = self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 1)
        self.assertFalse(report['traces']['losses'])

    def test_unconsumed_connection_shutdown_is_censored(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/')
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertFalse(capture.spans())
        self.assertEqual(report['traces']['losses'], {'http_incomplete': 1})

    def test_premature_second_request_preserves_the_first_pending_request(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/')
            with self.assertRaises(http.client.CannotSendRequest):
                connection.request('GET', '/empty')
            self.assertEqual(connection.getresponse().read(), b'line one\nline two\n')
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 1)
        self.assertFalse(report['traces']['losses'])

    def test_malformed_duplicate_and_state_only_application_fields_are_not_replaced(self):
        runtime, capture, _, monitor = self.client()
        parent = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-03'
        cases = [({'traceparent': 'malformed'}, ['malformed'], []),
                 ({'tracestate': 'orphan=1'}, [], ['orphan=1']),
                 ({'traceparent': parent, 'TraceParent': parent}, [parent, parent], [])]
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            for headers, parents, states in cases:
                connection.request('GET', '/capture', headers=headers)
                self.assertEqual(json.loads(connection.getresponse().read()),
                                 {'traceparent': parents, 'tracestate': states})
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 3)
        self.assertTrue(all(not span.links for span in capture.spans()))
        self.assertFalse(report['traces']['losses'])

    def test_unsampled_request_keeps_its_wire_identity_without_recorded_spans(self):
        policy = trace_plan()
        policy['traces']['root_sample_ratio'] = 0
        runtime, capture, _, monitor = self.client(policy)
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/capture')
            received = json.loads(connection.getresponse().read())
            connection.close()
        report = self.finish(runtime, monitor)
        parent = received['traceparent'][0].split('-')
        self.assertNotEqual(int(parent[1], 16), 0)
        self.assertNotEqual(int(parent[2], 16), 0)
        self.assertFalse(int(parent[3], 16) & 1)
        self.assertFalse(capture.spans())
        self.assertEqual(report['traces']['sampled_out_roots'], 1)
        self.assertFalse(report['traces']['losses'])

    def test_head_response_is_complete_before_any_body_read(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('HEAD', '/')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertFalse(monitor.http.clients.active_count)
            self.assertEqual(response.read(), b'')
            connection.close()
        self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 1)

    def test_transport_exception_object_and_payload_are_preserved(self):
        runtime, capture, _, monitor = self.client()
        connection = http.client.HTTPConnection('127.0.0.1', 1, timeout=3)
        original = OSError('application transport exception')

        def connect():
            raise original

        connection.connect = connect
        with self.assertRaises(OSError) as raised:
            connection.request('GET', '/')
        self.assertIs(raised.exception, original)
        self.assertFalse(monitor.http.clients.active_count)
        connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual([span.status.code for span in capture.spans()], [2])
        self.assertNotIn('application transport exception', str(capture.requests))
        self.assertFalse(report['traces']['losses'])

    def test_header_mapping_is_not_iterated_again_by_instrumentation(self):
        runtime, capture, _, monitor = self.client()
        calls = []

        class Headers(dict):
            def __iter__(self):
                calls.append('iterate')
                return super().__iter__()

            def items(self):
                calls.append('items')
                return super().items()

        parent = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-03'
        headers = Headers({'traceparent': parent})
        monitor.close()
        observations = []
        with plain_server() as port:
            for enabled in (False, True):
                if enabled:
                    monitor.install()
                before = len(calls)
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                connection.request('GET', '/capture', headers=headers)
                received = json.loads(connection.getresponse().read())
                self.assertEqual(received, {'traceparent': [parent], 'tracestate': []})
                connection.close()
                observations.append(calls[before:])
        self.finish(runtime, monitor)
        self.assertEqual(observations, [['iterate', 'items'], ['iterate', 'items']])
        self.assertEqual(len(capture.spans()), 1)

    def test_automatic_client_server_and_selected_function_spans_form_one_trace(self):
        runtime, capture, app, monitor = server_tests.PythonHTTPContextTests.monitored_server(self, client_hooks=True)
        exec(compile('def caller(action):\n    return action()\n',
                     str(server_tests.__file__).rsplit('/', 1)[0] + '/../trace_fixture.py', 'exec'), app)
        with server_tests.serve(app['Handler']) as (port, errors):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)

            def fetch():
                connection.request('GET', '/')
                return connection.getresponse().read()

            self.assertEqual(app['caller'](fetch), b'original response')
            connection.close()
        report = self.finish(runtime, monitor)
        spans = capture.spans()
        self.assertEqual(len({span.trace_id for span in spans}), 1)
        by_name = {span.name: span for span in spans}
        client, server = by_name['HTTP client'], by_name['HTTP server']
        self.assertEqual(client.parent_span_id, by_name['trace_fixture.caller'].span_id)
        self.assertEqual(server.parent_span_id, client.span_id)
        self.assertEqual(by_name['trace_fixture.Handler.do_GET'].parent_span_id, server.span_id)
        self.assertEqual(report['traces']['completed_trees'], 2)
        self.assertFalse(report['traces']['losses'])
        self.assertFalse(errors)

    def test_low_level_request_and_multi_value_headers_preserve_wire_state(self):
        runtime, capture, _, monitor = self.client()
        parent = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-03'
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.putrequest('GET', '/capture')
            connection.putheader('traceparent', parent)
            connection.putheader('tracestate', 'a=1', 'b=2')
            connection.endheaders()
            received = json.loads(connection.getresponse().read())
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual(received['traceparent'], [parent])
        self.assertEqual(received['tracestate'], ['a=1\r\n\tb=2'])
        span = capture.spans()[0]
        self.assertEqual(len(span.links), 1)
        self.assertFalse(span.links[0].trace_state)
        self.assertFalse(report['traces']['losses'])

    def test_low_level_request_injects_the_active_client_span(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.putrequest('GET', '/capture')
            connection.endheaders()
            received = json.loads(connection.getresponse().read())
            connection.close()
        self.finish(runtime, monitor)
        self.assertEqual(received['traceparent'][0].split('-')[2], capture.spans()[0].span_id.hex())

    def test_https_keeps_certificate_verification_and_records_handshake_failure(self):
        runtime, capture, _, monitor = self.client()
        with certificate() as tls, plain_server(tls) as port:
            trusted = ssl.create_default_context(cafile=str(tls[0]))
            connection = http.client.HTTPSConnection('localhost', port, timeout=3, context=trusted)
            connection.request('GET', '/')
            self.assertEqual(connection.getresponse().read(), b'line one\nline two\n')
            connection.close()
            connection = http.client.HTTPSConnection('localhost', port, timeout=3)
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.request('GET', '/')
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertEqual([span.status.code for span in capture.spans()], [0, 2])
        self.assertFalse(report['traces']['losses'])

    def test_urllib_success_and_http_error_keep_original_body_and_status(self):
        import urllib.error
        import urllib.request

        runtime, capture, _, monitor = self.client()
        with plain_server() as port:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=3) as response:
                self.assertEqual(response.read(), b'line one\nline two\n')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/error', timeout=3)
            self.assertEqual(error.exception.code, 503)
            self.assertEqual(error.exception.read(), b'line one\nline two\n')
            error.exception.close()
        report = self.finish(runtime, monitor)
        self.assertEqual([span.status.code for span in capture.spans()], [0, 2])
        self.assertFalse(report['traces']['losses'])

    def test_completed_metadata_does_not_form_a_cycle_when_gc_is_disabled(self):
        runtime, capture, _, monitor = self.client()
        enabled = gc.isenabled()
        gc.disable()
        try:
            with plain_server() as port:
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                connection.request('GET', '/')
                response = connection.getresponse()
                entry = monitor.http.clients.lookup(response, response=True)
                reference = weakref.ref(entry)
                del entry
                self.assertEqual(response.read(), b'line one\nline two\n')
                self.assertIsNone(reference())
                connection.close()
        finally:
            if enabled:
                gc.enable()
        self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 1)

    def test_shutdown_between_capture_and_admission_does_not_create_an_unobservable_scope(self):
        runtime, capture, _, monitor = self.client()
        clients = monitor.http.clients
        original = clients.capture

        def stop(frame):
            result = original(frame)
            clients.close()
            return result

        with plain_server() as port, patch.object(clients, 'capture', side_effect=stop):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/')
            self.assertEqual(connection.getresponse().read(), b'line one\nline two\n')
            connection.close()
        report = self.finish(runtime, monitor)
        self.assertFalse(clients.active_count)
        self.assertFalse(capture.spans())
        self.assertFalse(report['traces']['losses'])

    def test_client_capacity_rejection_is_counted_once_and_the_slot_recovers(self):
        policy = trace_plan()
        policy['runtime']['max_active_calls'] = 1
        runtime, capture, _, monitor = self.client(policy)
        with plain_server() as port:
            first = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            first.request('GET', '/')
            response = first.getresponse()
            second = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            second.request('GET', '/')
            self.assertEqual(second.getresponse().read(), b'line one\nline two\n')
            second.close()
            self.assertEqual(monitor.http.clients.active_count, 1)
            self.assertEqual(response.read(), b'line one\nline two\n')
            first.close()
            third = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            third.request('GET', '/')
            self.assertEqual(third.getresponse().read(), b'line one\nline two\n')
            third.close()
        report = self.finish(runtime, monitor)
        self.assertEqual(len(capture.spans()), 2)
        self.assertEqual(report['traces']['losses'], {'http_capacity': 1})

    def test_sdk_client_admission_fault_is_not_retried_by_the_nested_putrequest_hook(self):
        runtime, capture, _, monitor = self.client()
        with plain_server() as port, patch.object(runtime.traces, 'begin', side_effect=RuntimeError('private fault')) as begin:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('GET', '/')
            self.assertEqual(connection.getresponse().read(), b'line one\nline two\n')
            connection.close()
            self.assertEqual(begin.call_count, 1)
        report = self.finish(runtime, monitor)
        self.assertFalse(capture.spans())
        self.assertEqual(report['traces']['losses'], {'http_hook': 1})
