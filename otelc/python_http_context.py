"""Source-free urllib/stdlib-WSGI integration using official HTTP products."""

from __future__ import annotations

from functools import wraps
from http.client import HTTPConnection
from pathlib import Path
from urllib.request import OpenerDirector
from wsgiref.simple_server import WSGIServer

from opentelemetry import trace
from opentelemetry.instrumentation.urllib import URLLibInstrumentor

from otelc.python_http_product import products
from otelc.python_http_support import helpers, request_transport
from otelc.python_meter_bridge import BridgeMeterProvider
from otelc.python_runtime import PythonRuntime


class PythonHttpContext:
    """Own only installed patches; retain changed/foreign owners on shutdown."""

    def __init__(self, enabled: bool, runtime: PythonRuntime, directory: Path) -> None:
        self.runtime = runtime
        self._client = self._open = self._set_app = self._transport = None
        self._original_open = self._original_set_app = None
        self._original_transport = None
        if not enabled:
            return
        if (URLLibInstrumentor().is_instrumented_by_opentelemetry
                or getattr(OpenerDirector.open, 'opentelemetry_instrumentation_urllib_applied', False)):
            raise ValueError('Python HTTP instrumentation already has an owner')
        client, server = products(directory / 'http-products', helpers(runtime))
        tracer_provider = runtime._provider or trace.NoOpTracerProvider()
        meter_provider = BridgeMeterProvider(runtime)
        self._original_open = OpenerDirector.open
        self._original_set_app = WSGIServer.set_app
        self._original_transport = HTTPConnection.request
        self._client = client.URLLibInstrumentor()
        try:
            self._client.instrument(tracer_provider=tracer_provider, meter_provider=meter_provider)
            self._open = OpenerDirector.open
            if not self._client.is_instrumented_by_opentelemetry or self._open is self._original_open:
                raise RuntimeError('official Python HTTP instrumentation did not start')
            transport = self._original_transport

            @wraps(transport)
            def transport_request(instance, *args, **kwargs):
                return request_transport(transport, instance, args, kwargs)

            self._transport = transport_request
            HTTPConnection.request = transport_request
            original = self._original_set_app

            @wraps(original)
            def set_app(instance, application):
                return original(instance, server.OpenTelemetryMiddleware(application,
                    tracer_provider=tracer_provider, meter_provider=meter_provider))

            self._set_app = set_app
            WSGIServer.set_app = set_app
        except BaseException:
            candidate = OpenerDirector.open
            if (candidate is not self._original_open
                    and getattr(candidate, '__wrapped__', None) is self._original_open):
                self._open = candidate
            self.close()
            raise

    def close(self) -> None:
        """Do not unwrap patches installed by the application or another tool."""
        if self._transport is not None:
            if HTTPConnection.request is self._transport:
                HTTPConnection.request = self._original_transport
            else:
                self.runtime.reject('http_context_ownership')
            self._transport = None
        if self._set_app is not None:
            if WSGIServer.set_app is self._set_app:
                WSGIServer.set_app = self._original_set_app
            else:
                self.runtime.reject('http_context_ownership')
            self._set_app = None
        if self._client is not None:
            client, self._client = self._client, None
            if self._open is not None:
                if OpenerDirector.open is self._open:
                    if client.is_instrumented_by_opentelemetry:
                        client.uninstrument()
                    if OpenerDirector.open is self._open:
                        OpenerDirector.open = self._original_open
                else:
                    self.runtime.reject('http_context_ownership')
            self._open = None
