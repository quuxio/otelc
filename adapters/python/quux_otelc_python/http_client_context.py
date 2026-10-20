"""Bounded weak HTTP client scopes through response EOF or observed close."""
import contextlib
import functools
import http.client
import sys
import time
import weakref
from dataclasses import dataclass, field

from opentelemetry.trace import SpanKind, Status, StatusCode

from .carrier import extract, from_sdk, inject
from .deadlines import CURRENT
from .metadata import member
from .traces import SUPPRESSED, remote_context


_CONNECTION_DICT = http.client.HTTPConnection.__dict__['__dict__']
_RESPONSE_DICT = next(base.__dict__['__dict__'] for base in http.client.HTTPResponse.__mro__
                      if '__dict__' in base.__dict__)


@dataclass
class Request:
    token: object
    connection: object
    connection_id: int
    identity: object
    response: object = None
    response_id: int | None = None
    receiving: bool = False
    depth: int = 0
    closed: bool = False
    owned_headers: bool = False
    opaque_headers: bool = False
    sent: bool = False
    parents: list = field(default_factory=list)
    states: list = field(default_factory=list)
    header_bytes: int = 0


class HTTPClientContext:
    def __init__(self, context):
        self.context = context
        self.runtime = context.runtime
        self.entries = {}
        self.connections = {}
        self.responses = {}
        self.originals = {}
        self.wrappers = {}
        self.closed = False

    @property
    def active_count(self):
        return len(self.entries)

    def boundary(self, code):
        return any(code is wrapper.__code__ for wrapper in self.wrappers.values())

    def eligible(self, connection):
        if self.closed or self.runtime.closed or CURRENT.get(None) is not None:
            return False
        if type(connection) not in (http.client.HTTPConnection, http.client.HTTPSConnection):
            return False
        namespace = _CONNECTION_DICT.__get__(connection, type(connection))
        response_class = member(namespace, 'response_class', member(type(connection).__dict__, 'response_class',
                                member(http.client.HTTPConnection.__dict__, 'response_class')))
        if response_class is not http.client.HTTPResponse:
            self.context.loss('http_unsupported_client')
            return False
        return True

    def lookup(self, value, response=False):
        with self.runtime.lock:
            index = self.responses if response else self.connections
            entry = self.entries.get(index.get(id(value)))
            reference = entry.response if response and entry is not None else entry.connection if entry is not None else None
            return entry if reference is not None and reference() is value else None

    def capture(self, frame):
        monitor = self.runtime.monitor
        key = monitor.parent_key(frame) if monitor is not None else None
        with self.runtime.lock:
            if key is not None:
                parent = self.runtime.pending.get(key)
                return parent.trace if parent is not None else SUPPRESSED
            return monitor.parent_context() if monitor is not None else self.context.current()

    @contextlib.contextmanager
    def scope(self, identity):
        parent = self.context.current()
        entry = {}
        self.context.bind(entry, identity)
        try:
            yield
        finally:
            if entry.get('token') is not None:
                entry['context'].reset(entry['token'])
            self.context.local.identity = parent

    def admit(self, connection, parent):
        with self.runtime.lock:
            if self.closed or self.context.closed or self.runtime.closed:
                return None
            if len(self.runtime.pending) + self.context.active_count >= self.runtime.plan['runtime']['max_active_calls']:
                self.runtime.traces.reject(parent, 'http_capacity')
                return None
            identity = self.runtime.traces.begin(parent, 'HTTP client', time.perf_counter_ns(), kind=SpanKind.CLIENT)
            token = object()
            reference = weakref.ref(connection, lambda ref: self.expired(token, ref, False))
            entry = Request(token, reference, id(connection), identity)
            self.entries[token] = entry
            self.connections[id(connection)] = token
            return entry

    def span(self, entry):
        identity = entry.identity
        if not identity.sampled:
            return None
        tree = self.runtime.traces.roots.get(identity.local_root)
        return tree.nodes.get(identity.context.span_id) if tree is not None and not tree.invalid else None

    def finish(self, entry, failed=False, censored=False):
        with self.runtime.lock:
            if self.entries.pop(entry.token, None) is not entry:
                return
            for index, key in ((self.connections, entry.connection_id), (self.responses, entry.response_id)):
                if index.get(key) is entry.token:
                    index.pop(key)
            if censored:
                if entry.identity.sampled:
                    self.runtime.traces.reject(entry.identity, 'http_incomplete')
            elif failed:
                span = self.span(entry)
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR, 'HTTP client failure'))
            self.runtime.traces.finish(entry.identity, time.perf_counter_ns())

    def safe_finish(self, entry, failed=False, censored=False):
        try:
            self.finish(entry, failed, censored)
        except Exception:
            self.context.loss('http_hook')

    def expired(self, token, reference, response):
        with self.runtime.lock:
            entry = self.entries.get(token)
            if entry is None:
                return
            if reference is (entry.response if response else entry.connection) and (response or entry.response is None):
                self.safe_finish(entry, censored=True)

    def observe_header(self, entry, name, values):
        if type(name) is bytes:
            name = name.decode('ascii', errors='replace')
        if type(name) is not str:
            entry.opaque_headers = True
            return
        field_name = name.lower()
        if field_name not in ('traceparent', 'tracestate'):
            return
        entry.owned_headers = True
        fields = entry.parents if field_name == 'traceparent' else entry.states
        if field_name == 'tracestate' and len(values) != 1:
            fields.append(None)
            return
        for value in values:
            if len(fields) > 32:
                break
            if type(value) not in (str, bytes) or len(value) > 32768:
                fields.append(None)
                break
            entry.header_bytes += len(value)
            if entry.header_bytes > 32768:
                fields.append(None)
                break
            fields.append(value.decode('ascii', errors='replace') if type(value) is bytes else value)

    def headers(self, connection, entry):
        if entry.sent:
            return
        entry.sent = True
        if entry.owned_headers:
            remote = extract(entry.parents, entry.states)
            with self.runtime.lock:
                span = self.span(entry)
                if span is not None and remote is not None:
                    span.add_link(remote_context(remote))
                    tree = self.runtime.traces.roots[entry.identity.local_root]
                    tree.bytes += len(remote.tracestate or '') + 64
            return
        if entry.opaque_headers:
            self.context.loss('http_unsupported_headers')
            return
        value = from_sdk(entry.identity.context)
        if value is not None:
            for name, data in inject(value).items():
                self.originals[(http.client.HTTPConnection, 'putheader')](connection, name, data)

    def associate(self, response, entry):
        if type(response) is not http.client.HTTPResponse:
            self.safe_finish(entry, censored=True)
            return
        with self.runtime.lock:
            token = entry.token
            entry.response = weakref.ref(response, lambda ref: self.expired(token, ref, True))
            entry.response_id = id(response)
            self.responses[id(response)] = entry.token
            if self.connections.get(entry.connection_id) is entry.token:
                self.connections.pop(entry.connection_id)
            namespace = _RESPONSE_DICT.__get__(response, type(response))
            status = member(namespace, 'status')
            span = self.span(entry)
            if span is not None and type(status) is int:
                span.set_attribute('http.response.status_code', status)
                if status >= 400:
                    span.set_status(Status(StatusCode.ERROR, 'HTTP client error'))
            length = member(namespace, 'length')
            if type(length) is int and length == 0 or member(namespace, 'fp') is None:
                self.safe_finish(entry)

    def response_operation(self, response, original, args, kwargs):
        entry = self.lookup(response, response=True)
        if entry is None:
            return original(response, *args, **kwargs)
        entry.depth += 1
        failed = False
        try:
            with self.scope(entry.identity):
                return original(response, *args, **kwargs)
        except BaseException:
            failed = True
            raise
        finally:
            entry.depth -= 1
            if failed or entry.closed and not entry.depth:
                self.safe_finish(entry, failed=failed)

    def request_wrapper(self, original):
        @functools.wraps(original)
        def request(conn, *args, **kwargs):
            previous = getattr(self.context.local, 'client_request', None)
            if previous == id(conn):
                return original(conn, *args, **kwargs)
            self.context.local.client_request = id(conn)
            try:
                if not self.eligible(conn) or self.lookup(conn) is not None:
                    return original(conn, *args, **kwargs)
                parent = self.capture(sys._getframe())
                return self.request_operation(conn, original, parent, args, kwargs)
            finally:
                self.context.local.client_request = previous
        return request

    def request_operation(self, conn, original, parent, args, kwargs):
        try:
            entry = self.admit(conn, parent)
        except Exception:
            self.context.loss('http_hook')
            entry = None
        try:
            with self.scope(entry.identity if entry is not None else SUPPRESSED):
                return original(conn, *args, **kwargs)
        except BaseException:
            if entry is not None:
                self.safe_finish(entry, failed=True)
            raise

    def putheader_wrapper(self, original):
        @functools.wraps(original)
        def putheader(conn, name, *values):
            result = original(conn, name, *values)
            entry = self.lookup(conn)
            if entry is not None:
                try:
                    self.observe_header(entry, name, values)
                except Exception:
                    entry.opaque_headers = True
                    self.context.loss('http_hook')
            return result
        return putheader

    def endheaders_wrapper(self, original):
        @functools.wraps(original)
        def endheaders(conn, *args, **kwargs):
            entry = self.lookup(conn)
            if entry is None:
                return original(conn, *args, **kwargs)
            try:
                self.headers(conn, entry)
            except Exception:
                self.context.loss('http_hook')
            try:
                with self.scope(entry.identity):
                    return original(conn, *args, **kwargs)
            except BaseException:
                self.safe_finish(entry, failed=True)
                raise
        return endheaders

    def getresponse_wrapper(self, original):
        @functools.wraps(original)
        def getresponse(conn, *args, **kwargs):
            entry = self.lookup(conn)
            if entry is None:
                return original(conn, *args, **kwargs)
            entry.receiving = True
            try:
                with self.scope(entry.identity):
                    result = original(conn, *args, **kwargs)
            except BaseException:
                self.safe_finish(entry, failed=True)
                raise
            finally:
                entry.receiving = False
            try:
                self.associate(result, entry)
            except Exception:
                self.context.loss('http_hook')
                self.safe_finish(entry, censored=True)
            return result
        return getresponse

    def close_connection_wrapper(self, original):
        @functools.wraps(original)
        def close_connection(conn, *args, **kwargs):
            entry = self.lookup(conn)
            try:
                return original(conn, *args, **kwargs)
            finally:
                if entry is not None and not entry.receiving:
                    self.safe_finish(entry, censored=True)
        return close_connection

    def close_response_wrapper(self, original):
        @functools.wraps(original)
        def close_response(value, *args, **kwargs):
            entry = self.lookup(value, response=True)
            try:
                result = original(value, *args, **kwargs)
            except BaseException:
                if entry is not None:
                    self.safe_finish(entry, failed=True)
                raise
            if entry is not None:
                entry.closed = True
                if not entry.depth:
                    self.safe_finish(entry)
            return result
        return close_response

    def response_wrapper(self, original):
        @functools.wraps(original)
        def operation(value, *args, **kwargs):
            return self.response_operation(value, original, args, kwargs)
        return operation

    def install(self):
        connection, response = http.client.HTTPConnection, http.client.HTTPResponse
        factories = {(connection, 'request'): self.request_wrapper, (connection, 'putrequest'): self.request_wrapper,
                     (connection, 'putheader'): self.putheader_wrapper, (connection, 'endheaders'): self.endheaders_wrapper,
                     (connection, 'getresponse'): self.getresponse_wrapper, (connection, 'close'): self.close_connection_wrapper,
                     (response, '_close_conn'): self.close_response_wrapper}
        for name in ('read', 'readinto', 'read1', 'readline', 'readlines', 'peek', 'close'):
            factories[(response, name)] = self.response_wrapper
        originals = {key: getattr(*key) for key in factories}
        if any(getattr(value, '_otelc_http_context', False) for value in originals.values()):
            raise ValueError('HTTP client hook is already installed')
        self.originals = originals
        self.wrappers = {key: factory(originals[key]) for key, factory in factories.items()}
        for (cls, name), wrapper in self.wrappers.items():
            wrapper._otelc_http_context = True
            setattr(cls, name, wrapper)

    def close(self):
        self.closed = True
        for (cls, name), wrapper in self.wrappers.items():
            if getattr(cls, name) is wrapper:
                setattr(cls, name, self.originals[(cls, name)])
        with self.runtime.lock:
            for entry in tuple(self.entries.values()):
                self.safe_finish(entry, censored=True)
