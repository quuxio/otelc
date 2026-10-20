"""Retain SDK link state in otelc's own pinned OTLP exporter only."""
import threading

from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans as sdk_encode_spans
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace.export import SpanExportResult

from .carrier import from_sdk


MAX_BYTES = 16 * 1024 * 1024


def restore_link_states(request, spans):
    """Join by actual SDK span identity; reject mismatched encoder output."""
    source = {(span.context.trace_id, span.context.span_id): span for span in spans}
    if len(source) != len(spans):
        raise ValueError('duplicate SDK span identity in one export')
    for resource in request.resource_spans:
        for scope in resource.scope_spans:
            for span in scope.spans:
                original = source.pop((int.from_bytes(span.trace_id), int.from_bytes(span.span_id)))
                for encoded, link in zip(span.links, original.links, strict=True):
                    if (int.from_bytes(encoded.trace_id) != link.context.trace_id
                            or int.from_bytes(encoded.span_id) != link.context.span_id):
                        raise ValueError('SDK link identity changed during encoding')
                    context = from_sdk(link.context)
                    encoded.trace_state = context.tracestate or '' if context is not None else ''
    if source:
        raise ValueError('SDK omitted a span during encoding')
    return request


def encode_spans(spans):
    """Use the SDK encoder, retaining the validated link states it omits."""
    return restore_link_states(sdk_encode_spans(spans), spans)


class LinkStateClient:
    """Decorate this exporter's client, preserving its metrics/retry/shutdown path."""

    def __init__(self, client):
        self.client = client
        self.local = threading.local()

    def export(self, data):
        if self.local.spans is None:
            return self.client.export(data)
        request = ExportTraceServiceRequest.FromString(data)
        corrected = restore_link_states(request, self.local.spans).SerializeToString()
        if len(corrected) > MAX_BYTES:
            raise ValueError('trace export exceeds the wire byte limit')
        return self.client.export(corrected)

    def shutdown(self):
        self.client.shutdown()


class TraceExporter(OTLPSpanExporter):
    """SDK 1.45.1 exporter with an instance-local serialized-link correction."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The pinned SDK has no encoder argument. Decorate its owned client;
        # never replace the SDK module's global encoder or application exporters.
        self._wire = LinkStateClient(self._client)
        self._client = self._wire

    def export(self, spans):
        self._wire.local.spans = spans if any(link.context.trace_state for span in spans for link in span.links) else None
        try:
            return super().export(spans)
        except Exception:
            return SpanExportResult.FAILURE
        finally:
            del self._wire.local.spans
