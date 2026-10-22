"""Source-bound fixes preserve exceptions, iterator close and native responses."""

from __future__ import annotations

import random
import io
import tempfile
import threading
import unittest
import urllib.request
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import OpenerDirector, Request
from wsgiref.simple_server import WSGIServer

from opentelemetry import baggage, context, trace
from opentelemetry.instrumentation.urllib import URLLibInstrumentor
from opentelemetry.instrumentation.wsgi import OpenTelemetryMiddleware

from otelc.config import Policy
from otelc.python_http_context import PythonHttpContext
from otelc.python_http_product import products
from otelc.python_http_support import Response, helpers
from otelc.python_meter_bridge import BridgeMeterProvider
from otelc.python_runtime import PythonRuntime
from otelc.python_startup import PythonApplication
from otelc.python_sdk_process import SdkProcess
from test_java_integration import Capture


class PythonHttpComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        policy = Policy.parse({'schema_version': 3,
            'metrics': {'collection': False, 'emission': False},
            'traces': {'enabled': True, 'sample_ratio': 1.0},
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}', 'interval_ms': 100000}})
        self.sdk = SdkProcess.start(policy, self.root / 'sdk')
        self.addCleanup(self.sdk.abort)
        self.runtime = PythonRuntime(policy, self.sdk.channel)
        self.addCleanup(self.runtime.close)

    def finish(self):
        self.runtime.close()
        return self.sdk.wait_report()

    def server(self, length='1'):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append({'path': self.path, 'headers': dict(self.headers)})
                self.send_response(200)
                if length is not None:
                    self.send_header('Content-Length', length)
                self.end_headers()
                self.wfile.write(b'8')

            def log_message(self, *args):
                # The tests retain request evidence directly, not in stderr.
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/', received

    def test_unknown_response_size_is_not_reported_as_zero(self) -> None:
        url, _ = self.server(None)
        self.runtime.controls(True, True)
        owner = PythonHttpContext(True, self.runtime, self.root)
        self.addCleanup(owner.close)
        with urllib.request.urlopen(url) as response:
            self.assertEqual(response.read(), b'8')
        owner.close()
        self.assertEqual(self.finish()['phase'], 'closed')
        self.assertEqual(self.capture.points('http.client.response.body.size'), [])
        self.assertEqual(self.capture.points('http.client.request.duration')[-1].count, 1)

    def test_file_body_does_not_get_an_extra_len_call_or_fabricated_size(self) -> None:
        received = []

        class Body:
            def __init__(self):
                self.data = io.BytesIO(b'body')

            def read(self, size):
                return self.data.read(size)

            def __len__(self):
                raise AssertionError('native file transport does not ask len')

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200)
                self.send_header('Content-Length', '1')
                self.end_headers()
                self.wfile.write(b'8')

            def log_message(self, *args):
                # The fixture records actual payload bytes directly.
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}/'
        self.runtime.controls(True, True)
        for plain in (True, False):
            owner = None if plain else PythonHttpContext(True, self.runtime, self.root)
            try:
                request = Request(url, data=Body(), headers={'Content-Length': '4'})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.read(), b'8')
            finally:
                if owner is not None:
                    owner.close()
        self.assertEqual(received, [b'body', b'body'])
        self.assertEqual(self.finish()['phase'], 'closed')
        self.assertEqual(self.capture.points('http.client.request.body.size'), [])

    def test_official_exception_formatting_is_removed_without_changing_error_identity(self) -> None:
        reads = []

        class Failure(Exception):
            def __str__(self):
                reads.append('formatted')
                return 'original failure'

        failure = Failure()

        def application(environ, start_response):
            raise failure

        environ = {'REQUEST_METHOD': 'GET', 'SERVER_NAME': 'localhost', 'SERVER_PORT': '80',
                   'wsgi.url_scheme': 'http', 'PATH_INFO': '/failure', 'SERVER_PROTOCOL': 'HTTP/1.1'}
        native = trace.get_current_span().get_span_context()
        meter = BridgeMeterProvider(self.runtime)
        original = OpenTelemetryMiddleware(application, tracer_provider=self.runtime._provider, meter_provider=meter)
        with self.assertRaises(Failure) as raised:
            original(environ, lambda *args: None)
        self.assertIs(raised.exception, failure)
        self.assertTrue(reads, 'the official baseline must reproduce the extra formatting')
        reads.clear()
        _, server = products(self.root / 'products', helpers(self.runtime))
        patched = server.OpenTelemetryMiddleware(application, tracer_provider=self.runtime._provider, meter_provider=meter)
        with self.assertRaises(Failure) as raised:
            patched(environ, lambda *args: None)
        self.assertIs(raised.exception, failure)
        self.assertEqual(reads, [])
        self.assertEqual(trace.get_current_span().get_span_context(), native)
        self.assertEqual(self.finish()['phase'], 'closed')

    def test_response_close_runs_once_and_finishes_span_even_when_close_raises(self) -> None:
        failure = ValueError('original close')
        calls = []

        class Body:
            def __iter__(self):
                return iter((b'body',))

            def close(self):
                calls.append('closed')
                raise failure

        span = self.runtime._provider.get_tracer('fixture').start_span('response')
        response = Response(Body(), span, None)
        self.assertEqual(list(response), [b'body'])
        self.assertTrue(span.is_recording())
        with self.assertRaises(ValueError) as raised:
            response.close()
        self.assertIs(raised.exception, failure)
        response.close()
        self.assertEqual(calls, ['closed'])
        self.assertFalse(span.is_recording())
        self.assertEqual(self.finish()['phase'], 'closed')

    def test_invalid_content_length_keeps_native_body_and_records_observation_loss(self) -> None:
        url, _ = self.server('not-a-number')
        with urllib.request.urlopen(url) as response:
            self.assertEqual(response.read(), b'8')
        original_open, original_set_app = OpenerDirector.open, WSGIServer.set_app
        owner = PythonHttpContext(True, self.runtime, self.root)
        self.addCleanup(owner.close)
        with urllib.request.urlopen(url) as response:
            self.assertEqual(response.read(), b'8')
        owner.close()
        self.assertIs(OpenerDirector.open, original_open)
        self.assertIs(WSGIServer.set_app, original_set_app)
        self.assertEqual(self.runtime.losses, {'http_response_metadata': 1})
        self.assertEqual(self.finish()['phase'], 'failed')

    def test_caller_request_headers_keep_identity_order_and_values_after_injection(self) -> None:
        url, received = self.server()

        def request(cls=Request):
            value = cls(url, headers={'X-First': 'first', 'traceparent': '00-' + '1' * 32 + '-' + '2' * 16 + '-01',
                'tracestate': 'vendor=old', 'baggage': 'user=business', 'X-Last': 'last'})
            value.add_unredirected_header('traceparent', '00-' + '3' * 32 + '-' + '4' * 16 + '-01')
            return value

        def send(value):
            with urllib.request.urlopen(value) as response:
                self.assertEqual(response.read(), b'8')
            return list(value.headers.items()), list(value.unredirected_hdrs.items())

        expected = send(request())
        direct = request()
        with urllib.request.build_opener().open(direct) as response:
            self.assertEqual(response.read(), b'8')
        native_timeout = direct.timeout
        token = context.attach(baggage.set_baggage('session', 'ordinary'))
        self.addCleanup(context.detach, token)
        official = URLLibInstrumentor()
        official.instrument(tracer_provider=self.runtime._provider, meter_provider=BridgeMeterProvider(self.runtime))
        try:
            with self.runtime._provider.get_tracer('fixture').start_as_current_span('original'):
                self.assertNotEqual(send(request()), expected, 'the upstream mutation must be reproduced')
                direct = request()
                with urllib.request.build_opener().open(direct) as response:
                    self.assertEqual(response.read(), b'8')
                self.assertIsNot(direct.timeout, native_timeout, 'the upstream default-timeout change must reproduce')
        finally:
            official.uninstrument()
        owner = PythonHttpContext(True, self.runtime, self.root)
        self.addCleanup(owner.close)
        value = request()
        headers, unredirected = value.headers, value.unredirected_hdrs
        with self.runtime._provider.get_tracer('fixture').start_as_current_span('patched') as parent:
            self.assertEqual(send(value), expected)
            direct = request()
            with urllib.request.build_opener().open(direct) as response:
                self.assertEqual(response.read(), b'8')
            self.assertIs(direct.timeout, native_timeout)
        self.assertIs(value.headers, headers)
        self.assertIs(value.unredirected_hdrs, unredirected)
        wire_headers = {key.lower(): item for key, item in received[-1]['headers'].items()}
        self.assertEqual(wire_headers['baggage'], 'session=ordinary')
        self.assertNotIn('tracestate', wire_headers)
        trace_id, span_id = wire_headers['traceparent'].split('-')[1:3]
        self.assertEqual(trace_id, f'{parent.get_span_context().trace_id:032x}')
        owner.close()
        self.assertEqual(self.finish()['phase'], 'closed')
        spans = [span for message in self.capture.traces for resource in message.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertTrue(any(span.kind == 3 and span.span_id.hex() == span_id for span in spans))

    def test_request_subclasses_bypass_instrumentation_with_explicit_loss(self) -> None:
        url, _ = self.server()
        calls = []

        class CustomRequest(Request):
            def get_method(self):
                calls.append('method')
                return super().get_method()

        with urllib.request.urlopen(CustomRequest(url)) as response:
            self.assertEqual(response.read(), b'8')
        native_calls = list(calls)
        owner = PythonHttpContext(True, self.runtime, self.root)
        self.addCleanup(owner.close)
        calls.clear()
        with urllib.request.urlopen(CustomRequest(url)) as response:
            self.assertEqual(response.read(), b'8')
        self.assertEqual(calls, native_calls)
        owner.close()
        self.assertEqual(self.runtime.losses, {'http_request_shape': 1})
        self.assertEqual(self.finish()['phase'], 'failed')

    def test_parallel_transport_injection_keeps_requests_unmodified_in_flight(self) -> None:
        url, received = self.server()
        requests = [(name, Request(url + name, headers={'traceparent': 'original', 'X-Business': name}))
                    for name in ('first', 'second')]
        snapshots = [list(request.headers.items()) for _, request in requests]
        barrier = threading.Barrier(2)
        original = HTTPConnection.request
        inspected = []

        def observe(instance, *args, **kwargs):
            for (_, request), snapshot in zip(requests, snapshots):
                self.assertEqual(list(request.headers.items()), snapshot)
            packet = kwargs['headers'] if 'headers' in kwargs else args[3]
            self.assertNotEqual({key.lower(): value for key, value in packet.items()}['traceparent'], 'original')
            inspected.append(args[1])
            barrier.wait(timeout=5)
            return original(instance, *args, **kwargs)

        state = random.getstate()
        self.enterContext(mock.patch.object(HTTPConnection, 'request', new=observe))
        owner = PythonHttpContext(True, self.runtime, self.root)
        self.addCleanup(owner.close)
        errors, parents = [], {}

        def send(name, request):
            token = context.attach(baggage.set_baggage('request', name))
            try:
                with self.runtime._provider.get_tracer('fixture').start_as_current_span(name) as parent:
                    parents[name] = parent.get_span_context().trace_id
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.read(), b'8')
            except BaseException as error:
                errors.append(error)
            finally:
                context.detach(token)

        workers = [threading.Thread(target=send, args=values) for values in requests]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(set(inspected), {'/first', '/second'})
        for row in received:
            name = row['path'].removeprefix('/')
            headers = {key.lower(): value for key, value in row['headers'].items()}
            self.assertEqual(headers['traceparent'].split('-')[1], f'{parents[name]:032x}')
            self.assertEqual(headers['baggage'], 'request=' + name)
        self.assertEqual(random.getstate(), state)
        owner.close()
        self.assertEqual(self.finish()['phase'], 'closed')

    def test_disabled_existing_and_changed_owners_are_preserved(self) -> None:
        original_open, original_set_app = OpenerDirector.open, WSGIServer.set_app
        PythonHttpContext(False, self.runtime, self.root).close()
        instrumentor = URLLibInstrumentor()
        instrumentor.instrument(tracer_provider=self.runtime._provider, meter_provider=BridgeMeterProvider(self.runtime))
        def restore():
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
        self.addCleanup(restore)
        installed = OpenerDirector.open
        with self.assertRaisesRegex(ValueError, 'already has an owner'):
            PythonHttpContext(True, self.runtime, self.root)
        self.assertIs(OpenerDirector.open, installed)
        instrumentor.uninstrument()
        self.addCleanup(setattr, OpenerDirector, 'open', original_open)
        self.addCleanup(setattr, WSGIServer, 'set_app', original_set_app)
        owner = PythonHttpContext(True, self.runtime, self.root)
        foreign_open, foreign_set_app = mock.Mock(), mock.Mock()
        OpenerDirector.open, WSGIServer.set_app = foreign_open, foreign_set_app
        owner.close()
        self.assertIs(OpenerDirector.open, foreign_open)
        self.assertIs(WSGIServer.set_app, foreign_set_app)
        self.assertEqual(self.runtime.losses['http_context_ownership'], 2)

    def test_unqualified_source_is_refused_before_any_patch(self) -> None:
        original_open, original_set_app = OpenerDirector.open, WSGIServer.set_app
        with mock.patch.dict('otelc.python_http_product.SOURCES', {'urllib': '0' * 64}):
            with self.assertRaisesRegex(ValueError, 'unqualified official'):
                PythonHttpContext(True, self.runtime, self.root)
        self.assertIs(OpenerDirector.open, original_open)
        self.assertIs(WSGIServer.set_app, original_set_app)

    def test_partial_instrumentation_failure_restores_the_real_client_wrapper(self) -> None:
        original_open, original_set_app = OpenerDirector.open, WSGIServer.set_app
        client, server = products(self.root / 'products', helpers(self.runtime))
        instrumentor = client.URLLibInstrumentor()
        install = instrumentor._instrument

        def partially_install(**kwargs):
            install(**kwargs)
            self.assertIsNot(OpenerDirector.open, original_open)
            raise OSError('controlled partial installation')

        with mock.patch('otelc.python_http_context.products', return_value=(client, server)), \
                mock.patch.object(instrumentor, '_instrument', side_effect=partially_install):
            with self.assertRaisesRegex(OSError, 'partial installation'):
                PythonHttpContext(True, self.runtime, self.root)
        self.assertIs(OpenerDirector.open, original_open)
        self.assertIs(WSGIServer.set_app, original_set_app)
        self.assertFalse(instrumentor.is_instrumented_by_opentelemetry)

    def test_http_shutdown_failure_does_not_skip_the_actual_sdk_checkpoint(self) -> None:
        http = mock.Mock()
        http.close.side_effect = OSError('controlled HTTP shutdown failure')
        application = PythonApplication(self.runtime, mock.Mock(), mock.Mock(), http=http)
        application.close()
        application.close()
        http.close.assert_called_once()
        report = self.sdk.wait_report()
        self.assertEqual(report['phase'], 'failed')
        self.assertTrue(report['producer_checkpoint_available'])
        self.assertEqual(report['producer']['runtime_losses']['http_context_shutdown'], 1)
