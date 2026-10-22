"""Public OpenTelemetry metric API gates for the Python adapter.

Aggregation and transport stay in the official SDK. Collection transitions
invalidate callback generations; already admitted measurements retain SDK
cumulative semantics. No SDK private meter configurator is used.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics
from opentelemetry.context import Context
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.util.types import Attributes

Callback = Callable[[metrics.CallbackOptions], Iterable[metrics.Observation]]
CallbackSource = Callback | Generator[
    Iterable[metrics.Observation], metrics.CallbackOptions, None
]


@dataclass(frozen=True)
class MetricState:
    """An atomic live-control snapshot, without runtime credentials."""

    collection: bool
    emission: bool
    generation: int


class MetricControls:
    """Keep collection and emission independent and invalidate late callbacks."""

    def __init__(self, collection: bool, emission: bool) -> None:
        if type(collection) is not bool or type(emission) is not bool:
            raise ValueError('metric switches must be boolean')
        self._lock = threading.RLock()
        self._state = MetricState(False, False, 0)
        self.update(collection=collection, emission=emission)

    def snapshot(self) -> MetricState:
        """Read both switches and their collection generation atomically."""
        with self._lock:
            return self._state

    def update(self, *, collection: bool | None = None,
               emission: bool | None = None) -> MetricState:
        """Apply one or both switches without replacing meters or instruments."""
        for value in (collection, emission):
            if value is not None and type(value) is not bool:
                raise ValueError('metric switches must be boolean')
        with self._lock:
            current = self._state
            collecting = current.collection if collection is None else collection
            emitting = current.emission if emission is None else emission
            self._state = MetricState(
                collecting, emitting,
                current.generation + int(collecting != current.collection),
            )
            return self._state

    def accepts(self, generation: int) -> bool:
        """Admit only points from the same enabled collection generation."""
        state = self.snapshot()
        return state.collection and state.generation == generation


class _SynchronousGate:
    def __init__(self, delegate: Any, controls: MetricControls) -> None:
        self.delegate = delegate
        self.controls = controls


class _AddGate(_SynchronousGate):
    def add(self, amount: float, attributes: Attributes = None,
            context: Context | None = None) -> None:
        if self.controls.snapshot().collection:
            self.delegate.add(amount, attributes=attributes, context=context)


class _Counter(_AddGate, metrics.Counter):
    """A cached counter whose enabled state is read at each measurement."""


class _UpDownCounter(_AddGate, metrics.UpDownCounter):
    """A cached up/down counter with the same public API gate."""


class _Histogram(_SynchronousGate, metrics.Histogram):
    def record(self, amount: float, attributes: Attributes = None,
               context: Context | None = None) -> None:
        if self.controls.snapshot().collection:
            self.delegate.record(amount, attributes=attributes, context=context)


# The pinned API exports its experimental gauge type as `_Gauge` in __all__.
class _Gauge(_SynchronousGate, metrics._Gauge):
    def set(self, amount: float, attributes: Attributes = None,
            context: Context | None = None) -> None:
        if self.controls.snapshot().collection:
            self.delegate.set(amount, attributes=attributes, context=context)


class _CallbackGate:
    def __init__(self, callback: CallbackSource,
                 controls: MetricControls) -> None:
        self.callback = callback
        self.controls = controls
        self.primed = False

    def __call__(self, options: metrics.CallbackOptions
                 ) -> Iterable[metrics.Observation]:
        state = self.controls.snapshot()
        if not state.collection:
            return
        if isinstance(self.callback, Generator):
            try:
                if not self.primed:
                    next(self.callback)
                    self.primed = True
                values = self.callback.send(options)
            except StopIteration:
                return
        else:
            values = self.callback(options)
        for observation in values:
            if not self.controls.accepts(state.generation):
                return
            yield observation


class GatedMeter(metrics.Meter):
    """Delegate SDK registration and gate all public measurement entry points."""

    def __init__(self, delegate: metrics.Meter,
                 controls: MetricControls) -> None:
        super().__init__(delegate.name, delegate.version, delegate.schema_url)
        self._delegate = delegate
        self._controls = controls
        self._wrappers: dict[object, Any] = {}
        self._wrapper_lock = threading.Lock()

    def _wrap(self, instrument: Any, wrapper: type) -> Any:
        with self._wrapper_lock:
            if instrument not in self._wrappers:
                self._wrappers[instrument] = wrapper(instrument, self._controls)
            return self._wrappers[instrument]

    def create_counter(self, name: str, unit: str = '',
                       description: str = '') -> metrics.Counter:
        """Create or retrieve a counter with normal SDK registration rules."""
        return self._wrap(
            self._delegate.create_counter(name, unit, description), _Counter,
        )

    def create_up_down_counter(self, name: str, unit: str = '',
                               description: str = '') -> metrics.UpDownCounter:
        """Gate signed increments, including instruments cached while off."""
        return self._wrap(
            self._delegate.create_up_down_counter(name, unit, description),
            _UpDownCounter,
        )

    def create_histogram(
        self, name: str, unit: str = '', description: str = '', *,
        explicit_bucket_boundaries_advisory: Sequence[float] | None = None,
    ) -> metrics.Histogram:
        """Preserve SDK histogram advice and aggregation."""
        return self._wrap(self._delegate.create_histogram(
            name, unit, description,
            explicit_bucket_boundaries_advisory=(
                explicit_bucket_boundaries_advisory
            ),
        ), _Histogram)

    def create_gauge(self, name: str, unit: str = '',
                     description: str = '') -> metrics._Gauge:
        """Gate synchronous gauge updates without replacing the SDK gauge."""
        return self._wrap(
            self._delegate.create_gauge(name, unit, description), _Gauge,
        )

    def _callbacks(self, callbacks: Sequence[CallbackSource] | None
                   ) -> list[Callback]:
        return [_CallbackGate(callback, self._controls)
                for callback in callbacks or ()]

    def create_observable_counter(
        self, name: str, callbacks: Sequence[CallbackSource] | None = None,
        unit: str = '', description: str = '',
    ) -> metrics.ObservableCounter:
        """Suppress callback invocation and invalidate cross-transition points."""
        return self._delegate.create_observable_counter(
            name, self._callbacks(callbacks), unit, description,
        )

    def create_observable_up_down_counter(
        self, name: str, callbacks: Sequence[CallbackSource] | None = None,
        unit: str = '', description: str = '',
    ) -> metrics.ObservableUpDownCounter:
        """Retain the SDK's signed observable-counter semantics."""
        return self._delegate.create_observable_up_down_counter(
            name, self._callbacks(callbacks), unit, description,
        )

    def create_observable_gauge(
        self, name: str, callbacks: Sequence[CallbackSource] | None = None,
        unit: str = '', description: str = '',
    ) -> metrics.ObservableGauge:
        """Create an SDK observable gauge with controlled callbacks."""
        return self._delegate.create_observable_gauge(
            name, self._callbacks(callbacks), unit, description,
        )


class GatedMeterProvider(metrics.MeterProvider):
    """Wrap meters through the public API, preserving SDK scope identity."""

    def __init__(self, delegate: metrics.MeterProvider,
                 controls: MetricControls) -> None:
        self._delegate = delegate
        self._controls = controls
        self._meters: dict[metrics.Meter, GatedMeter] = {}
        self._lock = threading.Lock()

    def get_meter(self, name: str, version: str | None = None,
                  schema_url: str | None = None,
                  attributes: Attributes = None) -> GatedMeter:
        """Obtain a stable wrapper for each underlying SDK meter scope."""
        delegate = self._delegate.get_meter(
            name, version, schema_url, attributes,
        )
        with self._lock:
            if delegate not in self._meters:
                self._meters[delegate] = GatedMeter(delegate, self._controls)
            return self._meters[delegate]


class GatedMetricExporter(MetricExporter):
    """Gate new SDK exports and retain actual API outcomes separately."""

    def __init__(self, delegate: MetricExporter,
                 controls: MetricControls) -> None:
        # Default SDK reader temporality is cumulative for every instrument.
        # It retains observations collected while emission is disabled.
        super().__init__()
        self._delegate = delegate
        self._controls = controls
        self._started = self._succeeded = self._failed = self._pending = 0
        self._flush_started = self._flush_failed = self._flush_pending = 0
        self._closed = False
        self._shutdown_done = threading.Event()
        self._shutdown_failed = False

    def status(self) -> dict[str, int | bool]:
        """Report raw producer acknowledgements, without a receipt claim."""
        with self._controls._lock:
            state = self._controls.snapshot()
            return {
                'collection': state.collection, 'emission': state.emission,
                'export_started': self._started,
                'export_succeeded': self._succeeded,
                'export_failed': self._failed,
                'in_flight_exports': self._pending,
                'flush_started': self._flush_started,
                'flush_failed': self._flush_failed,
                'in_flight_flushes': self._flush_pending,
                'effective_emission_off': (
                    not state.emission and self._pending == 0
                ),
                'closed': self._closed,
                'shutdown_complete': self._shutdown_done.is_set(),
                'shutdown_failed': self._shutdown_failed,
            }

    def export(self, metrics_data: MetricsData,
               timeout_millis: float = 10_000,
               **kwargs: Any) -> MetricExportResult:
        """Admit under the control lock, then call transport without that lock."""
        with self._controls._lock:
            state = self._controls.snapshot()
            if self._closed:
                return MetricExportResult.FAILURE
            if not state.collection or not state.emission:
                return MetricExportResult.SUCCESS
            self._started += 1
            self._pending += 1
        result = MetricExportResult.FAILURE
        try:
            result = self._delegate.export(
                metrics_data, timeout_millis=timeout_millis, **kwargs,
            )
            return result
        finally:
            with self._controls._lock:
                self._pending -= 1
                if result is MetricExportResult.SUCCESS:
                    self._succeeded += 1
                else:
                    self._failed += 1

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        """Keep the delegate's flush result; callers also inspect outcomes."""
        with self._controls._lock:
            self._flush_started += 1
            self._flush_pending += 1
        succeeded = False
        try:
            succeeded = self._delegate.force_flush(
                timeout_millis=timeout_millis,
            ) is True
            return succeeded
        finally:
            with self._controls._lock:
                self._flush_pending -= 1
                self._flush_failed += int(not succeeded)

    def shutdown(self, timeout_millis: float = 30_000,
                 **kwargs: Any) -> None:
        """Delegate shutdown once; repeated callers wait for its real result."""
        # SDK 1.45.1's periodic reader supplies `timeout`, while the public
        # exporter API names this argument `timeout_millis`. Keep that budget.
        if 'timeout' in kwargs:
            timeout_millis = min(timeout_millis, kwargs.pop('timeout'))
        with self._controls._lock:
            first = not self._closed
            self._closed = True
        if not first:
            if not self._shutdown_done.wait(max(0, timeout_millis) / 1000):
                raise TimeoutError('metric exporter shutdown is still pending')
            if self._shutdown_failed:
                raise RuntimeError('metric exporter shutdown failed')
            return
        try:
            self._delegate.shutdown(timeout_millis=timeout_millis, **kwargs)
        except Exception:
            with self._controls._lock:
                self._shutdown_failed = True
            raise
        finally:
            self._shutdown_done.set()
