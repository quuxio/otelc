"""Private standard-library HTTP request scopes; no application object registry."""
import functools
import email.message
import http.client
import http.server
import socketserver
import threading
import time

from opentelemetry.trace import SpanKind, Status, StatusCode

from .carrier import extract
from .metadata import member
from .traces import SUPPRESSED


_HANDLER_DICT = socketserver.BaseRequestHandler.__dict__['__dict__']
_MESSAGE_DICT = email.message.Message.__dict__['__dict__']


def request_fields(handler):
    """Read parser-owned storage through builtin descriptors, never user getters."""
    headers = member(_HANDLER_DICT.__get__(handler, type(handler)), 'headers')
    if type(headers) is not http.client.HTTPMessage:
        return None
    values = member(_MESSAGE_DICT.__get__(headers, type(headers)), '_headers')
    if type(values) is not list or len(values) > 100:
        return None
    parents, states = [], []
    for pair in values:
        if (type(pair) is not tuple or len(pair) != 2
                or type(pair[0]) is not str or type(pair[1]) is not str):
            return None
        name, value = pair
        if name.lower() == 'traceparent':
            parents.append(value)
        elif name.lower() == 'tracestate':
            states.append(value)
    return parents, states


class HTTPContext:
    """Own bounded local HTTP trees independently of distributed trace IDs."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.local = threading.local()
        self.active = {}
        self.previous = {}
        self.wrappers = {}
        self.closed = False

    def current(self):
        return getattr(self.local, 'identity', None)

    def boundary(self, code):
        return any(code is wrapper.__code__ for wrapper in self.wrappers.values())

    @property
    def active_count(self):
        return len(self.active)

    def loss(self, reason):
        with self.runtime.lock:
            self.runtime.traces.losses[reason] += 1

    def enter_server(self, handler, entry):
        if self.closed or self.runtime.closed:
            return
        fields = request_fields(handler)
        if fields is None:
            self.loss('http_unsupported_headers')
            self.bind(entry, SUPPRESSED)
            return
        remote = extract(*fields)
        with self.runtime.lock:
            if len(self.runtime.pending) + self.active_count >= self.runtime.plan['runtime']['max_active_calls']:
                self.loss('http_capacity')
                identity = SUPPRESSED
            else:
                identity = self.runtime.traces.begin(None, 'HTTP server', time.perf_counter_ns(),
                                                     remote=remote, kind=SpanKind.SERVER)
                entry['key'] = object()
                self.active[entry['key']] = identity
            self.bind(entry, identity)

    def bind(self, entry, identity):
        entry['identity'] = identity
        self.local.identity = identity
        monitor = self.runtime.monitor
        context = monitor.context if monitor is not None else None
        if context is not None:
            entry['context'] = context.current
            entry['token'] = context.current.set(identity)

    def status(self, entry, code):
        if entry is None or type(code) not in (int, http.HTTPStatus):
            return
        identity = entry.get('identity')
        if identity is None or not identity.sampled:
            return
        with self.runtime.lock:
            tree = self.runtime.traces.roots.get(identity.local_root)
            span = tree.nodes.get(identity.context.span_id) if tree is not None and not tree.invalid else None
            if span is not None:
                span.set_attribute('http.response.status_code', int(code))
                if code >= 500:
                    span.set_status(Status(StatusCode.ERROR, 'HTTP server error'))

    def handled(self, code, exception):
        """Observe the standard handler's swallowed timeout, not application catches."""
        original = self.previous.get('handle_one_request')
        if original is None or code is not original.__code__ or not isinstance(exception, TimeoutError):
            return
        entry = getattr(self.local, 'server', None)
        identity = entry.get('identity') if entry is not None else None
        if identity is None or not identity.sampled:
            return
        with self.runtime.lock:
            tree = self.runtime.traces.roots.get(identity.local_root)
            span = tree.nodes.get(identity.context.span_id) if tree is not None and not tree.invalid else None
            if span is not None:
                span.set_status(Status(StatusCode.ERROR, 'HTTP timeout'))

    def finish_server(self, entry, failed):
        if entry.get('token') is not None:
            entry['context'].reset(entry['token'])
        with self.runtime.lock:
            identity = self.active.pop(entry.get('key'), None)
            if identity is not None:
                self.runtime.traces.finish(identity, time.perf_counter_ns(), failed)

    def install(self):
        handler = http.server.BaseHTTPRequestHandler
        names = ('handle_one_request', 'parse_request', 'send_response_only')
        originals = {name: getattr(handler, name) for name in names}
        if self.runtime.http is not None or any(getattr(value, '_otelc_http_context', False) for value in originals.values()):
            raise ValueError('HTTP context hook is already installed')

        @functools.wraps(originals['handle_one_request'])
        def handle_one_request(connection, *args, **kwargs):
            previous = getattr(self.local, 'server', None)
            parent = self.current()
            entry = {}
            self.local.server = entry
            failed = False
            try:
                return originals['handle_one_request'](connection, *args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                try:
                    self.finish_server(entry, failed)
                except Exception:
                    self.loss('http_hook')
                self.local.server = previous
                self.local.identity = parent

        @functools.wraps(originals['parse_request'])
        def parse_request(connection, *args, **kwargs):
            result = originals['parse_request'](connection, *args, **kwargs)
            entry = getattr(self.local, 'server', None)
            if result and entry is not None:
                try:
                    self.enter_server(connection, entry)
                except Exception:
                    self.loss('http_hook')
                    self.bind(entry, SUPPRESSED)
            return result

        @functools.wraps(originals['send_response_only'])
        def send_response_only(connection, code, *args, **kwargs):
            result = originals['send_response_only'](connection, code, *args, **kwargs)
            try:
                self.status(getattr(self.local, 'server', None), code)
            except Exception:
                self.loss('http_hook')
            return result

        self.previous = originals
        self.wrappers = dict(zip(names, (handle_one_request, parse_request, send_response_only)))
        for name, wrapper in self.wrappers.items():
            wrapper._otelc_http_context = True
            setattr(handler, name, wrapper)
        self.runtime.http = self

    def close(self):
        self.closed = True
        for name, wrapper in self.wrappers.items():
            if getattr(http.server.BaseHTTPRequestHandler, name) is wrapper:
                setattr(http.server.BaseHTTPRequestHandler, name, self.previous[name])
        with self.runtime.lock:
            for identity in self.active.values():
                if identity.sampled:
                    try:
                        self.runtime.traces.reject(identity, 'http_incomplete')
                        self.runtime.traces.finish(identity, time.perf_counter_ns())
                    except Exception:
                        self.loss('http_hook')
            self.active.clear()
        if self.runtime.http is self:
            self.runtime.http = None
