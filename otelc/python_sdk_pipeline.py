"""Official Python SDK aggregation and batch export owned by the SDK worker."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor, SpanExporter, SpanExportResult,
)
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from otelc.config import Policy
from otelc.python_metrics import (
    GatedMeterProvider, GatedMetricExporter, MetricControls,
)
from otelc import python_sdk_protocol as wire
from otelc.python_sdk_reader import AccountedMetricReader


def span_context(value: dict[str, Any] | None) -> trace.SpanContext | None:
    """Reconstitute the source's identity; do not invent a replacement span."""
    if value is None:
        return None
    encoded = value.get('trace_state', '')
    state = trace.TraceState.from_header([encoded])
    if state.to_header() != encoded:
        raise ValueError('invalid Python SDK trace state')
    return trace.SpanContext(
        int(value['trace_id'], 16), int(value['span_id'], 16),
        value['remote'], trace.TraceFlags(value['trace_flags']), state,
    )


class WireSpan(ReadableSpan):
    """Preserve already-observed SDK drop metadata on a completed span."""

    def __init__(self, value: dict[str, Any], resource: Resource) -> None:
        self._source_drops = {key: value.get('dropped_' + key, 0)
                              for key in ('attributes', 'events', 'links')}
        super().__init__(
            name=value['name'], context=span_context(value['context']),
            parent=span_context(value.get('parent')), resource=resource,
            attributes=value.get('attributes', {}),
            events=[Event(event['name'], event.get('attributes', {}),
                          event['timestamp'])
                    for event in value.get('events', [])],
            links=[trace.Link(span_context(link['context']),
                              link.get('attributes', {}))
                   for link in value.get('links', [])],
            kind=trace.SpanKind[value.get('span_kind', 'INTERNAL')],
            status=trace.Status(trace.StatusCode[value.get('status', 'UNSET')],
                                value.get('description')),
            start_time=value['start_time'], end_time=value['end_time'],
            instrumentation_scope=InstrumentationScope(
                value.get('scope', 'io.quux.otelc.functions'),
                value.get('version'), value.get('schema_url'),
                value.get('scope_attributes'),
            ),
        )

    @property
    def dropped_attributes(self) -> int:
        return self._source_drops['attributes']

    @property
    def dropped_events(self) -> int:
        return self._source_drops['events']

    @property
    def dropped_links(self) -> int:
        return self._source_drops['links']


class AccountedSpanExporter(SpanExporter):
    """Count SDK exporter API outcomes, not downstream receipt."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate
        self._lock = threading.RLock()
        self._started = self._succeeded = self._failed = self._pending = 0
        self._items = self._exported_items = self._failed_items = 0
        self._pending_items = 0
        self._closed = False

    def snapshot(self) -> dict[str, int | bool]:
        with self._lock:
            return {'started': self._started, 'succeeded': self._succeeded,
                    'failed': self._failed, 'pending': self._pending,
                    'started_items': self._items,
                    'exported_items': self._exported_items,
                    'failed_items': self._failed_items,
                    'pending_items': self._pending_items, 'closed': self._closed}

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            if self._closed:
                return SpanExportResult.FAILURE
            self._started += 1
            self._pending += 1
            self._items += len(spans)
            self._pending_items += len(spans)
        result = SpanExportResult.FAILURE
        try:
            result = self._delegate.export(spans)
            return result
        finally:
            with self._lock:
                self._pending -= 1
                self._pending_items -= len(spans)
                if result is SpanExportResult.SUCCESS:
                    self._succeeded += 1
                    self._exported_items += len(spans)
                else:
                    self._failed += 1
                    self._failed_items += len(spans)

    def force_flush(self, timeout_millis: float = 30_000) -> bool:
        return self._delegate.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._delegate.shutdown()


class SdkPipeline:
    """Translate common policy into owned official SDK components."""

    def __init__(self, policy: Policy, launch_id: str, application_pid: int, *,
                 metric_exporter: MetricExporter | None = None,
                 span_exporter: SpanExporter | None = None) -> None:
        data = policy.data
        destination = data['export']
        self.controls = MetricControls(
            data['metrics']['collection'], data['metrics']['emission'],
        )
        self._traces_enabled = data['traces']['enabled']
        self._phase = 'running'
        self._lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._shutdown_ms = destination['shutdown_timeout_ms']
        self._timeout_ms = destination['timeout_ms']
        self._resource = Resource.create({
            'service.name': data['resource']['service_name'],
            'service.instance.id': launch_id, 'process.pid': application_pid,
            'otelc.language': 'python',
        })
        endpoint = destination['endpoint'].rstrip('/')
        self._metric_exporter = GatedMetricExporter(metric_exporter if metric_exporter is not None else OTLPMetricExporter(
            endpoint=endpoint + '/v1/metrics', timeout=self._timeout_ms / 1000,
            headers={},
        ), self.controls)
        self._reader = AccountedMetricReader(
            self._metric_exporter, interval_ms=destination['interval_ms'],
            timeout_ms=self._timeout_ms,
        )
        self._metrics = MeterProvider(
            metric_readers=[self._reader], resource=self._resource,
            shutdown_on_exit=False, views=[View(
                instrument_name=name,
                aggregation=ExplicitBucketHistogramAggregation(
                    data['metrics']['histogram_boundaries_seconds'],
                ),
            ) for name in ('otelc.function.duration', 'otelc.lifetime.duration')],
        )
        self._meters = GatedMeterProvider(self._metrics, self.controls)
        self._trace_exporter = AccountedSpanExporter(span_exporter if span_exporter is not None else OTLPSpanExporter(
            endpoint=endpoint + '/v1/traces', timeout=self._timeout_ms / 1000,
            headers={},
        ))
        self._processor = BatchSpanProcessor(
            self._trace_exporter, max_queue_size=2048,
            max_export_batch_size=512, export_timeout_millis=self._timeout_ms,
            schedule_delay_millis=destination['interval_ms'],
        )
        self._instruments: dict[str, Any] = {}
        self._received_spans = 0
        self._reader.start()

    def snapshot(self) -> dict[str, Any]:
        """Only a completed idle SDK shutdown can establish queue counts."""
        with self._lock:
            phase = self._phase
            received = self._received_spans
        traces = self._trace_exporter.snapshot()
        metrics = self._metric_exporter.status()
        collection = self._reader.snapshot()
        known = phase == 'closed' and not traces['failed'] and not traces['pending']
        return {**metrics, 'phase': phase, 'traces': traces,
                'metric_collection': collection,
                'span_accounting': {
                    'received': received, 'sdk_exported': traces['exported_items'],
                    'queue_evidence': 'derived_after_shutdown' if known else 'unavailable',
                    'queue_pending': 0 if known else None,
                    'queue_dropped': received - traces['started_items'] if known else None,
                    'downstream_receipt': 'unavailable',
                    'otlp_partial_rejection': 'unavailable',
                }}

    def metric(self, value: dict[str, Any]) -> None:
        """Record an application-side primitive into the owned SDK meter."""
        with self._lock:
            if self._phase != 'running':
                raise RuntimeError('Python SDK pipeline is not running')
        wire.metric(value)
        registration = {key: value.get(key) for key in (
            'scope', 'version', 'schema_url', 'scope_attributes',
            'instrument', 'name', 'unit', 'description',
        )}
        key = json.dumps(registration, sort_keys=True, separators=(',', ':'))
        instrument = self._instruments.get(key)
        if instrument is None:
            if len(self._instruments) >= wire.MAX_INSTRUMENTS:
                raise ValueError('Python SDK instrument capacity exceeded')
            meter = self._meters.get_meter(
                value['scope'], value.get('version'), value.get('schema_url'),
                value.get('scope_attributes'),
            )
            create = getattr(meter, 'create_' + value['instrument'])
            instrument = create(value['name'], value.get('unit', ''),
                                value.get('description', ''))
            self._instruments[key] = instrument
        context = span_context(value.get('context'))
        active = trace.set_span_in_context(trace.NonRecordingSpan(context)) if context else None
        method = {'counter': 'add', 'up_down_counter': 'add',
                  'histogram': 'record', 'gauge': 'set'}[value['instrument']]
        getattr(instrument, method)(value['value'], value.get('attributes'), active)

    def span(self, value: dict[str, Any]) -> None:
        """Queue a completed sampled span independently of metric switches."""
        if not self._traces_enabled:
            raise ValueError('Python SDK tracing is disabled by policy')
        wire.span(value)
        if not value['context']['trace_flags'] & 1:
            raise ValueError('Python SDK span handoff must be sampled')
        span = WireSpan(value, self._resource)
        with self._lock:
            if self._phase != 'running':
                raise RuntimeError('Python SDK pipeline is not running')
            self._received_spans += 1
        self._processor.on_end(span)

    def flush(self) -> None:
        """Check real outcomes after the SDK barriers, without requiring global idle."""
        with self._operation_lock:
            if self._phase != 'running':
                raise RuntimeError('Python SDK pipeline is not running')
            before = self.snapshot()
            try:
                if self._metrics.force_flush(timeout_millis=self._timeout_ms) is not True:
                    raise RuntimeError('Python SDK metric flush failed')
                if not self._processor.force_flush(timeout_millis=self._timeout_ms):
                    raise RuntimeError('Python SDK trace flush failed')
            except Exception:
                raise RuntimeError('Python SDK flush failed') from None
            self._verify(before, idle=False)

    def _verify(self, before: dict[str, Any], *, idle: bool) -> None:
        after = self.snapshot()
        failed = (after['export_failed'] != before['export_failed']
                  or after['flush_failed'] != before['flush_failed']
                  or after['traces']['failed'] != before['traces']['failed']
                  or after['metric_collection']['failed'] != before['metric_collection']['failed'])
        pending = (after['in_flight_exports'] or after['in_flight_flushes']
                   or after['traces']['pending'] or after['metric_collection']['pending'])
        if failed or idle and pending:
            raise RuntimeError('Python SDK operation failed or remains pending')

    def shutdown(self) -> None:
        """Bound the owner wait; timed-out SDK operations remain unproven."""
        with self._shutdown_lock:
            if self._phase == 'closed':
                return
            if self._phase == 'failed':
                raise RuntimeError('Python SDK shutdown previously failed')
            with self._operation_lock:
                self._phase = 'closing'
                before = self.snapshot()
                self._stop_components()
                try:
                    self._verify(before, idle=True)
                except Exception:
                    self._phase = 'failed'
                    raise
                self._phase = 'closed'

    def _stop_components(self) -> None:
        results: list[bool] = []
        lock = threading.Lock()
        done = threading.Event()

        def run(operation):
            succeeded = False
            try:
                operation()
                succeeded = True
            except Exception:
                # The owner records failure without formatting SDK exceptions.
                pass
            finally:
                with lock:
                    results.append(succeeded)
                    if len(results) == 2:
                        done.set()

        for operation in (
            lambda: self._metrics.shutdown(timeout_millis=self._shutdown_ms),
            self._processor.shutdown,
        ):
            threading.Thread(target=run, args=(operation,), daemon=True).start()
        if not done.wait(self._shutdown_ms / 1000) or not all(results):
            self._phase = 'failed'
            raise RuntimeError('Python SDK shutdown failed or exceeded its deadline')
