"""Preserve native errors and response cleanup around official HTTP products."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from inspect import signature
from typing import Any, Iterator
from urllib.error import HTTPError
from urllib.request import OpenerDirector, Request

from opentelemetry import context, propagate, trace

from otelc.python_runtime import PythonRuntime, exception_type


def error(span: trace.Span, exception: BaseException) -> None:
    """Do not call application str/repr, descriptors or exception formatters."""
    if span.is_recording():
        span.set_status(trace.StatusCode.ERROR)
        span.add_event('exception', {'exception.type': exception_type(exception)})


@contextmanager
def use_span(span: trace.Span) -> Iterator[trace.Span]:
    with trace.use_span(span, record_exception=False, set_status_on_exception=False):
        try:
            yield span
        except BaseException as exception:
            error(span, exception)
            raise


@contextmanager
def current_span(tracer: trace.Tracer, *args: Any, **kwargs: Any) -> Iterator[trace.Span]:
    span = tracer.start_span(*args, **kwargs)
    try:
        with use_span(span):
            yield span
    finally:
        span.end()


def error_result(exception: BaseException) -> Any:
    """Only the standard HTTP error has native response metadata to inspect."""
    return object.__getattribute__(exception, 'file') if type(exception) is HTTPError else None


class TransportHeaders:
    """Carry context to the transport without changing application requests."""

    def __init__(self, runtime: PythonRuntime, previous: TransportHeaders | None) -> None:
        self.runtime = runtime
        self.values = dict(previous.values) if previous is not None else {}

    def inject(self, headers: dict[str, str]) -> None:
        fresh: dict[str, str] = {}
        propagate.inject(fresh)
        self.values = {}
        for key, value in fresh.items():
            if type(key) is not str or type(value) is not str:
                self.runtime.reject('http_propagator_headers')
                continue
            self.values[key.lower()] = value

    def combine(self, headers: Any) -> dict[str, Any] | None:
        if type(headers) is not dict or any(type(key) is not str for key in headers):
            self.runtime.reject('http_transport_headers')
            return None
        removed = set(self.values)
        if 'traceparent' in self.values:
            removed.add('tracestate')
        return {**{key: value for key, value in headers.items() if key.lower() not in removed},
                **self.values}


TRANSPORT_HEADERS: ContextVar[TransportHeaders | None] = ContextVar('otelc_http_transport_headers', default=None)


def request_transport(original: Any, instance: Any, args: tuple, kwargs: dict) -> Any:
    """Alter only the owned native transport's temporary header dictionary."""
    carrier = TRANSPORT_HEADERS.get()
    if carrier is None or not carrier.values:
        return original(instance, *args, **kwargs)
    headers = kwargs['headers'] if 'headers' in kwargs else args[3] if len(args) > 3 else {}
    combined = carrier.combine(headers)
    if combined is None:
        return original(instance, *args, **kwargs)
    if 'headers' in kwargs or len(args) <= 3:
        return original(instance, *args, **{**kwargs, 'headers': combined})
    return original(instance, *args[:3], combined, *args[4:], **kwargs)


class Response:
    """A WSGI server closes the iterable once, even when iteration/close fails."""

    def __init__(self, iterable: Any, span: trace.Span, token: Any) -> None:
        self._iterable = iterable
        self._iterator = iter(iterable)
        self._span = span
        self._token = token
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        with trace.use_span(self._span, record_exception=False, set_status_on_exception=False):
            try:
                return next(self._iterator)
            except StopIteration:
                raise
            except BaseException as exception:
                error(self._span, exception)
                raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            with use_span(self._span):
                close = getattr(self._iterable, 'close', None)
                if close is not None:
                    close()
        finally:
            try:
                self._span.end()
            finally:
                if self._token is not None:
                    context.detach(self._token)
                self._iterable = self._iterator = self._token = None


def helpers(runtime: PythonRuntime) -> dict[str, Any]:
    def request_input(value: Any) -> bool:
        if type(value) is str:
            return True
        if type(value) is Request:
            carriers = (value.headers, value.unredirected_hdrs)
            if all(type(carrier) is dict and all(type(key) is str for key in carrier) for carrier in carriers):
                return True
        runtime.reject('http_request_shape')
        return False

    @contextmanager
    def transport_context() -> Iterator[TransportHeaders]:
        carrier = TransportHeaders(runtime, TRANSPORT_HEADERS.get())
        token = TRANSPORT_HEADERS.set(carrier)
        try:
            yield carrier
        finally:
            TRANSPORT_HEADERS.reset(token)

    def request_size(data: Any) -> int | None:
        if data is None:
            return 0
        if type(data) is bytes or type(data) is bytearray:
            return len(data)
        if type(data) is memoryview:
            return data.nbytes
        return None

    def content_length(response: Any) -> int | None:
        value = response.headers.get('Content-Length')
        if value is None:
            return None
        try:
            if type(value) is not str:
                raise ValueError('non-string Content-Length')
            size = int(value)
            if size < 0:
                raise ValueError('negative Content-Length')
            return size
        except ValueError:
            runtime.reject('http_response_metadata')
            return None

    return {'_otelc_use_span': use_span, '_otelc_current_span': current_span,
            '_otelc_exception_type': exception_type, '_otelc_response': Response,
            '_otelc_error_result': error_result, '_otelc_content_length': content_length,
            '_otelc_request_input': request_input, '_otelc_transport_context': transport_context,
            '_otelc_open_timeout': signature(OpenerDirector.open).parameters['timeout'].default,
            '_otelc_request_size': request_size}
