"""SDK spans buffered as bounded complete local trees, without application frames."""
import collections
import ipaddress
import math
import time
import urllib.parse
import requests
from dataclasses import dataclass, field

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Link, NonRecordingSpan, Status, StatusCode

from .telemetry import Session, export_headers


@dataclass(frozen=True)
class SpanContext:
    context: object = None
    sampled: bool = False


SUPPRESSED = SpanContext()


@dataclass
class Tree:
    root: int
    origin: int
    epoch: int
    nodes: dict = field(default_factory=dict)
    active: int = 1
    closed: bool = False
    invalid: bool = False
    bytes: int = 0


class TraceSession(Session):
    ack_type = ExportTraceServiceResponse
    rejected_field = "rejected_spans"

    def __init__(self, store):
        super().__init__()
        self.store = store

    def request(self, *args, **kwargs):
        # The SDK merges environment headers even with explicit arguments.
        # Use only our validated signal-specific policy, including an empty override.
        self.headers.clear()
        kwargs["headers"] = {"Content-Type": "application/x-protobuf", **self.store.headers}
        deadline = self.store.deadline
        if self.store.runtime.shutdown_deadline is not None:
            deadline = min(deadline, self.store.runtime.shutdown_deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("trace export deadline elapsed")
        kwargs["timeout"] = min(kwargs.get("timeout", remaining), remaining)
        try:
            return super().request(*args, **kwargs)
        except (requests.RequestException, UnicodeError, ValueError):
            raise requests.RequestException("OTLP trace request failed") from None


class TraceStore:
    def __init__(self, runtime, resource, delegate=None):
        plan = runtime.plan
        policy, export = plan["traces"], plan.get("trace_export", {})
        try:
            ratio = policy["root_sample_ratio"]
            active, spans = policy["max_active_traces"], policy["max_spans_per_trace"]
            queued, timeout = plan["export"]["max_queued_batches"], export["timeout_ms"]
            if (not math.isfinite(ratio) or not 0 <= ratio <= 1
                    or any(type(value) is not int for value in (active, spans, queued, timeout))
                    or not 1 <= active <= 65536 or not 1 <= spans <= 65536
                    or active * spans > 1048576 or not 1 <= queued <= 64
                    or not 1 <= timeout <= 60000 or export["protocol"] != "http/protobuf"):
                raise ValueError
            url = urllib.parse.urlsplit(export["endpoint"])
            loopback = url.hostname == "localhost"
            if not loopback:
                try:
                    loopback = ipaddress.ip_address(url.hostname).is_loopback
                except ValueError:
                    pass
            if (url.scheme not in ("http", "https") or not url.hostname
                    or url.username is not None or url.password is not None or url.query or url.fragment
                    or url.scheme == "http" and not loopback):
                raise ValueError
            _ = url.port
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("invalid resolved Python trace settings") from None
        self.runtime, self.policy = runtime, policy
        self.timeout_ms = timeout
        self.capacity = queued
        self.roots = {}
        self.ready = collections.deque()
        self.leases = {}
        self.next_lease = 0
        self.retained = self.completed = self.sampled_out = 0
        self.losses = collections.Counter()
        self.deadline = float("inf")
        self.headers = export_headers("OTEL_EXPORTER_OTLP_TRACES_HEADERS")
        self.provider = TracerProvider(
            resource=resource, sampler=ParentBased(TraceIdRatioBased(ratio)),
            shutdown_on_exit=False,
            span_limits=SpanLimits(max_attributes=3 if plan.get('lifetimes', {}).get('enabled') else 2,
                                   max_events=0, max_links=1 if plan.get('lifetimes', {}).get('enabled') else 0,
                                   max_span_attribute_length=1024))
        self.tracer = self.provider.get_tracer("quux.otelc", "0.1.0")
        self.delegate = delegate if delegate is not None else OTLPSpanExporter(
            endpoint=export["endpoint"], headers=self.headers,
            timeout=min(timeout, plan["runtime"]["shutdown_timeout_ms"]) / 1000,
            session=TraceSession(self), compression=Compression.NoCompression,
            max_request_size=16 * 1024 * 1024)

    def begin_lifetime(self, name, token, creation, wall):
        links = [Link(creation.context)] if creation is not None and creation.sampled else []
        return self.tracer.start_span(name + ' lifetime', context=Context(), links=links, start_time=wall,
                                      attributes={'code.type.name': name, 'otelc.lifetime.boundary': 'collection',
                                                  'otelc.lifetime.instance': str(token)})

    def finish_lifetime(self, span, elapsed):
        if not span.is_recording():
            return
        span.end(end_time=span.start_time + elapsed)
        if len(self.ready) >= self.capacity or self.retained >= 1048576:
            self.losses['queue_capacity'] += 1
            return
        identity = span.get_span_context()
        tree = Tree(identity.span_id, 0, span.start_time, nodes={identity.span_id: span},
                    active=0, closed=True, bytes=len(span.name.encode()) * 2 + 512)
        self.retained += 1
        self.completed += 1
        self.ready.append(tree)

    def reject(self, parent, reason):
        if parent is not None and parent.sampled:
            tree = self.roots.get(parent.context.trace_id)
            if tree is not None and not tree.invalid:
                tree.invalid = True
                self.retained -= len(tree.nodes)
                tree.nodes = {}
                self.losses[reason] += 1
        elif parent is None:
            self.losses[reason] += 1
        return SUPPRESSED

    def begin(self, parent, name, started):
        if parent is not None:
            if not parent.sampled:
                return SUPPRESSED
            tree = self.roots.get(parent.context.trace_id)
            if tree is None:
                self.losses["context_expired"] += 1
                return SUPPRESSED
            if tree.invalid:
                return SUPPRESSED
            if len(tree.nodes) >= self.policy["max_spans_per_trace"] or self.retained >= 1048576:
                return self.reject(parent, "span_capacity")
            context = trace.set_span_in_context(NonRecordingSpan(parent.context), Context())
            wall = tree.epoch + max(0, started - tree.origin)
        else:
            context, wall = Context(), time.time_ns()
        span = self.tracer.start_span(name, context=context, start_time=wall,
                                      attributes={"code.function.name": name})
        identity = span.get_span_context()
        if not identity.is_valid:
            return self.reject(parent, "invalid")
        if not identity.trace_flags.sampled:
            self.sampled_out += 1
            return SUPPRESSED
        result = SpanContext(identity, True)
        if parent is None:
            if len(self.roots) >= self.policy["max_active_traces"] or self.retained >= 1048576:
                return self.reject(None, "trace_capacity")
            if identity.trace_id in self.roots:
                return self.reject(None, "invalid")
            tree = Tree(identity.span_id, started, wall)
            self.roots[identity.trace_id] = tree
        else:
            if identity.span_id in tree.nodes:
                return self.reject(parent, "invalid")
            tree.active += 1
        tree.nodes[identity.span_id] = span
        tree.bytes += len(name.encode()) * 2 + 256
        self.retained += 1
        return result

    def acquire(self, parent):
        """Reserve a continuation before its creator can finish; caller holds the runtime lock."""
        if parent is None or not parent.sampled:
            return None
        tree = self.roots.get(parent.context.trace_id)
        if tree is None or tree.invalid:
            return None
        if len(self.leases) >= self.runtime.plan["runtime"]["max_active_calls"]:
            self.reject(parent, "context_capacity")
            return None
        self.next_lease += 1
        self.leases[self.next_lease] = parent
        tree.active += 1
        return self.next_lease

    def release(self, lease):
        parent = self.leases.pop(lease, None)
        if parent is None:
            return
        tree = self.roots.get(parent.context.trace_id)
        if tree is not None:
            tree.active -= 1
            self.complete(parent.context.trace_id, tree)

    def finish(self, identity, ended, unwound=False, cancelled=False):
        if identity is None or not identity.sampled:
            return
        tree = self.roots.get(identity.context.trace_id)
        if tree is None:
            return
        if not tree.invalid:
            span = tree.nodes.get(identity.context.span_id)
            if span is None or span.end_time is not None:
                return
            if cancelled:
                span.set_attribute("otelc.cancelled", True)
            if unwound or cancelled:
                span.set_status(Status(StatusCode.ERROR, "cancelled" if cancelled else "escaping unwind"))
            span.end(end_time=max(span.start_time, tree.epoch + max(0, ended - tree.origin)))
        tree.active -= 1
        if identity.context.span_id == tree.root:
            tree.closed = True
        self.complete(identity.context.trace_id, tree)

    def complete(self, trace_id, tree):
        if tree.active or not tree.closed:
            return
        self.roots.pop(trace_id)
        if tree.invalid:
            return
        if len(self.ready) >= self.capacity:
            self.retained -= len(tree.nodes)
            self.losses["queue_capacity"] += 1
        else:
            self.completed += 1
            self.ready.append(tree)

    def flush(self):
        self.deadline = time.monotonic() + self.timeout_ms / 1000
        for _ in range(self.capacity):
            with self.runtime.lock:
                if not self.ready:
                    break
                tree = self.ready.popleft()
                self.retained -= len(tree.nodes)
                if tree.bytes > 16 * 1024 * 1024:
                    self.losses["batch_bytes"] += 1
                    continue
            try:
                if (time.monotonic() >= self.deadline
                        or self.runtime.shutdown_deadline is not None
                        and time.monotonic() >= self.runtime.shutdown_deadline):
                    raise TimeoutError
                result = self.delegate.export(tuple(tree.nodes.values()))
            except Exception:
                result = SpanExportResult.FAILURE
            if result != SpanExportResult.SUCCESS:
                self.runtime.export_loss += 1

    def shutdown_pending(self):
        for tree in self.roots.values():
            self.retained -= len(tree.nodes)
            if not tree.invalid:
                self.losses["incomplete"] += 1
        self.roots.clear()
        self.leases.clear()

    def close(self):
        self.delegate.shutdown()
        self.provider.shutdown()

    def report(self):
        return {"completed_trees": self.completed, "sampled_out_roots": self.sampled_out,
                "active_trees": len(self.roots), "queued_trees": len(self.ready),
                "pending_contexts": len(self.leases), "losses": dict(self.losses)}
