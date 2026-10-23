"""Application-side official tracing with primitive handoff, not SDK transport."""

from __future__ import annotations

import os
import threading
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from otelc.python_handoff import HandoffWriter


class OsIdGenerator(IdGenerator):
    """Generate valid OTel IDs without consuming the application's random state."""

    @staticmethod
    def _nonzero(size: int) -> int:
        value = 0
        while not value:
            value = int.from_bytes(os.urandom(size), 'big')
        return value

    def generate_span_id(self) -> int:
        return self._nonzero(8)

    def generate_trace_id(self) -> int:
        return self._nonzero(16)

    def is_trace_id_random(self) -> bool:
        return True


def encoded_context(value: trace.SpanContext | None) -> dict[str, Any] | None:
    """Keep the SDK's existing IDs, flags and trace state across the pipe."""
    if value is None:
        return None
    return {'trace_id': f'{value.trace_id:032x}', 'span_id': f'{value.span_id:016x}',
            'trace_flags': int(value.trace_flags), 'remote': value.is_remote,
            'trace_state': value.trace_state.to_header()}


def primitive_attributes(value: Any) -> dict[str, Any]:
    """Convert SDK-frozen attribute arrays to the primitive wire representation."""
    return {key: list(item) if type(item) is tuple else item
            for key, item in (value or {}).items()}


def encoded_span(span: ReadableSpan) -> dict[str, Any]:
    """Copy SDK span data without formatting any application exception/object."""
    scope = span.instrumentation_scope
    return {
        'name': span.name, 'context': encoded_context(span.context),
        'parent': encoded_context(span.parent), 'start_time': span.start_time,
        'end_time': span.end_time, 'span_kind': span.kind.name,
        'attributes': primitive_attributes(span.attributes),
        'events': [{'name': event.name, 'timestamp': event.timestamp,
                    'attributes': primitive_attributes(event.attributes),
                    'dropped_attributes': event.dropped_attributes}
                   for event in span.events],
        'links': [{'context': encoded_context(link.context),
                   'attributes': primitive_attributes(link.attributes),
                   'dropped_attributes': link.dropped_attributes}
                  for link in span.links],
        'scope': scope.name, 'version': scope.version or '',
        'schema_url': scope.schema_url or '',
        'scope_attributes': primitive_attributes(scope.attributes),
        'status': span.status.status_code.name,
        'description': span.status.description or '',
        'dropped_attributes': span.dropped_attributes,
        'dropped_events': span.dropped_events, 'dropped_links': span.dropped_links,
    }


class HandoffSpanProcessor(SpanProcessor):
    """Count sampled observations separately from complete pipe acceptance."""

    def __init__(self, writer: HandoffWriter) -> None:
        self.writer = writer
        self._lock = threading.Lock()
        self._started = self._ended = self._handed_off = self._errors = 0

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if span.context.trace_flags.sampled:
            with self._lock:
                self._started += 1

    def on_end(self, span: ReadableSpan) -> None:
        if not span.context.trace_flags.sampled:
            return
        accepted = False
        error = False
        try:
            accepted = self.writer.send({'kind': 'span', 'span': encoded_span(span)})
        except Exception:
            # The processor boundary must not replace an application's return
            # or exception. The final checkpoint keeps this failure visible.
            error = True
        finally:
            with self._lock:
                self._ended += 1
                self._handed_off += int(accepted)
                self._errors += int(error)

    def snapshot(self) -> tuple[dict[str, int], int]:
        with self._lock:
            return ({'sampled_started': self._started, 'sampled_ended': self._ended,
                     'handed_off': self._handed_off}, self._errors)


def provider(writer: HandoffWriter, ratio: float) -> tuple[TracerProvider, HandoffSpanProcessor]:
    """Use public SDK sampling/context with no application-side export thread."""
    result = TracerProvider(resource=Resource({}), shutdown_on_exit=False,
        sampler=ParentBased(TraceIdRatioBased(ratio)), id_generator=OsIdGenerator())
    processor = HandoffSpanProcessor(writer)
    result.add_span_processor(processor)
    return result, processor
