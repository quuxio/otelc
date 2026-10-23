"""Library metric API instruments hand off primitives to the owned SDK."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.context import Context

from otelc import python_sdk_protocol as wire
from otelc.python_runtime import PythonRuntime
from otelc.python_tracing import encoded_context


def primitive_attributes(value: Any) -> dict[str, Any]:
    """Copy the primitive attribute profile without invoking opaque mappings."""
    if value is None:
        return {}
    if type(value) is not dict:
        raise ValueError('unsupported library metric attribute mapping')
    result = {key: list(item) if type(item) is tuple else item
              for key, item in value.items()}
    return wire.attributes(result)


class _Instrument:
    def __init__(self, runtime: PythonRuntime, registration: dict[str, Any]) -> None:
        options = {'explicit_bucket_boundaries_advisory': tuple(registration['boundaries'])} if 'boundaries' in registration else {}
        super().__init__(registration['name'], registration['unit'], registration['description'], **options)
        self.name, self.unit, self.description = (registration[key] for key in ('name', 'unit', 'description'))
        self._runtime = runtime
        self._registration = registration

    def _record(self, value: int | float, labels: Any, active: Context | None) -> None:
        runtime = self._runtime
        with runtime._lock:
            if runtime.closed or not runtime.collection:
                return
            try:
                span = trace.get_current_span(active).get_span_context()
                record = {**self._registration, 'value': value,
                    'attributes': primitive_attributes(labels),
                    'context': encoded_context(span) if span.is_valid else None}
                wire.metric(record)
                runtime.channel.writer.send(record)
            except (ValueError, TypeError, OverflowError):
                runtime.reject('library_metric')


class _Add(_Instrument):
    def add(self, amount: int | float, attributes: Any = None,
            context: Context | None = None) -> None:
        self._record(amount, attributes, context)


class _Counter(_Add, metrics.Counter):
    """A stable public counter; admission is checked for each measurement."""


class _UpDownCounter(_Add, metrics.UpDownCounter):
    """Signed synchronous observations keep their original SDK instrument kind."""


class _Histogram(_Instrument, metrics.Histogram):
    def record(self, amount: int | float, attributes: Any = None,
               context: Context | None = None) -> None:
        self._record(amount, attributes, context)


class _Gauge(_Instrument, metrics._Gauge):
    def set(self, amount: int | float, attributes: Any = None,
            context: Context | None = None) -> None:
        self._record(amount, attributes, context)


WRAPPERS = {'counter': _Counter, 'up_down_counter': _UpDownCounter,
            'histogram': _Histogram, 'gauge': _Gauge}


class BridgeMeter(metrics.Meter):
    """The library gets API instruments; only the SDK child aggregates values."""

    def __init__(self, provider: BridgeMeterProvider, scope: dict[str, Any]) -> None:
        super().__init__(scope['scope'], scope['version'], scope['schema_url'])
        self._provider = provider
        self._scope = scope
        self._noop = metrics.NoOpMeter(self.name, self.version, self.schema_url)

    def _create(self, kind: str, name: str, unit: str, description: str,
                boundaries: Sequence[float] | None = None) -> Any:
        registration = {**self._scope, 'kind': 'metric', 'instrument': kind,
                        'name': name, 'unit': unit, 'description': description}
        try:
            if boundaries is not None:
                if type(boundaries) is not tuple and type(boundaries) is not list:
                    raise ValueError('unsupported histogram boundaries')
                registration['boundaries'] = list(boundaries)
            wire.metric({**registration, 'value': 0})
            return self._provider.instrument(registration)
        except (ValueError, TypeError, OverflowError):
            self._provider.runtime.reject('library_instrument')
            return getattr(self._noop, 'create_' + kind)('otelc.invalid')

    def create_counter(self, name: str, unit: str = '', description: str = '') -> metrics.Counter:
        return self._create('counter', name, unit, description)

    def create_up_down_counter(self, name: str, unit: str = '', description: str = '') -> metrics.UpDownCounter:
        return self._create('up_down_counter', name, unit, description)

    def create_histogram(self, name: str, unit: str = '', description: str = '', *,
                         explicit_bucket_boundaries_advisory: Sequence[float] | None = None) -> metrics.Histogram:
        return self._create('histogram', name, unit, description, explicit_bucket_boundaries_advisory)

    def create_gauge(self, name: str, unit: str = '', description: str = '') -> metrics._Gauge:
        return self._create('gauge', name, unit, description)

    def _observable(self, kind: str, name: str, unit: str, description: str) -> Any:
        # HTTP libraries use synchronous instruments. A missing application-side
        # callback scheduler must remain a loss, not a silent successful no-op.
        self._provider.runtime.reject('library_observable_unsupported')
        return getattr(self._noop, 'create_observable_' + kind)(name, (), unit, description)

    def create_observable_counter(self, name: str, callbacks: Any = None, unit: str = '', description: str = '') -> metrics.ObservableCounter:
        return self._observable('counter', name, unit, description)

    def create_observable_up_down_counter(self, name: str, callbacks: Any = None, unit: str = '', description: str = '') -> metrics.ObservableUpDownCounter:
        return self._observable('up_down_counter', name, unit, description)

    def create_observable_gauge(self, name: str, callbacks: Any = None, unit: str = '', description: str = '') -> metrics.ObservableGauge:
        return self._observable('gauge', name, unit, description)


class BridgeMeterProvider(metrics.MeterProvider):
    """Bound registrations and preserve library scope and instrument metadata."""

    def __init__(self, runtime: PythonRuntime) -> None:
        self.runtime = runtime
        self._lock = threading.RLock()
        self._meters: dict[str, BridgeMeter] = {}
        self._instruments: dict[str, Any] = {}

    def get_meter(self, name: str, version: str | None = None,
                  schema_url: str | None = None, attributes: Any = None) -> metrics.Meter:
        try:
            scope = {'scope': wire.text(name, limit=1024),
                     'version': wire.text(version if version is not None else '', limit=128, empty=True),
                     'schema_url': wire.text(schema_url if schema_url is not None else '', limit=1024, empty=True),
                     'scope_attributes': primitive_attributes(attributes)}
            key = json.dumps(scope, sort_keys=True)
            with self._lock:
                if key not in self._meters:
                    if len(self._meters) >= wire.MAX_INSTRUMENTS:
                        raise ValueError('library meter capacity exceeded')
                    self._meters[key] = BridgeMeter(self, scope)
                return self._meters[key]
        except (ValueError, TypeError, OverflowError):
            self.runtime.reject('library_meter')
            return metrics.NoOpMeter('otelc.invalid')

    def instrument(self, registration: dict[str, Any]) -> Any:
        key = json.dumps(registration, sort_keys=True)
        with self._lock:
            if key not in self._instruments:
                if len(self._instruments) >= wire.MAX_INSTRUMENTS:
                    raise ValueError('library instrument capacity exceeded')
                self._instruments[key] = WRAPPERS[registration['instrument']](self.runtime, registration)
            return self._instruments[key]
