"""Live application observations; aggregation and export belong to the SDK child."""

from __future__ import annotations

import os
import threading
import time
from contextvars import Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentelemetry import context, trace
from opentelemetry.context import Context

from otelc.config import Policy
from otelc.python_sdk_process import SdkChannel
from otelc import python_tracing
from otelc.python_lifetimes import Lifetimes

MAX_ACTIVE = 65536


@dataclass
class Invocation:
    name: str
    started: int
    collect: bool
    span: trace.Span | None
    token: Token[Context] | None = None
    code_id: int | None = None


def exception_type(error: BaseException) -> str:
    """Read the raw type name without str/repr or application attribute hooks."""
    name = type.__dict__['__qualname__'].__get__(type(error))
    return name if type(name) is str and len(name.encode()) <= 1024 else 'BaseException'


class PythonRuntime:
    """Order metric switches with observations while keeping tracing independent."""

    def __init__(self, policy: Policy, channel: SdkChannel, *, root: Path | None = None) -> None:
        self.channel = channel
        self._lock = threading.RLock()
        self._control_lock = threading.Lock()
        self.collection = policy.data['metrics']['collection']
        self.emission = policy.data['metrics']['emission']
        self.closed = False
        self.completed = 0
        self.losses: dict[str, int] = {}
        self.pending: dict[int, Invocation] = {}
        self._provider = self._processor = self._tracer = None
        if policy.data['traces']['enabled']:
            self._provider, self._processor = python_tracing.provider(
                channel.writer, policy.data['traces']['sample_ratio'])
            self._tracer = self._provider.get_tracer('io.quux.otelc.functions')
        self.lifetimes = None
        if policy.data['lifetimes']['enabled']:
            if root is None:
                raise ValueError('Python lifetime observations require an application root')
            self.lifetimes = Lifetimes(policy, root, self)

    def reject(self, reason: str) -> None:
        with self._lock:
            self.losses[reason] = self.losses.get(reason, 0) + 1

    def abandon(self, key: int) -> None:
        """Remove a failed probe's context without inventing a completed span."""
        with self._lock:
            if call := self.pending.pop(key, None):
                self._detach(call)
                self.reject('incomplete')

    def enter(self, key: int, name: str, *, code_id: int | None = None) -> None:
        with self._lock:
            if self.closed:
                return
            if key in self.pending or len(self.pending) >= MAX_ACTIVE:
                self.reject('active_capacity')
                return
            attributes: dict[str, str | int] = {'code.function.name': name}
            if code_id is not None:
                attributes['otelc.python.code_id'] = code_id
            span = self._tracer.start_span(name, attributes=attributes) if self._tracer else None
            call = Invocation(name, time.perf_counter_ns(), self.collection, span, code_id=code_id)
            self.pending[key] = call
            self._attach(call)

    @staticmethod
    def _attach(call: Invocation) -> None:
        if call.span is not None and call.token is None:
            call.token = context.attach(trace.set_span_in_context(call.span))

    @staticmethod
    def _detach(call: Invocation) -> None:
        if call.token is not None:
            context.detach(call.token)
            call.token = None

    def suspend(self, key: int) -> None:
        with self._lock:
            if call := self.pending.get(key):
                self._detach(call)

    def resume(self, key: int) -> None:
        with self._lock:
            if call := self.pending.get(key):
                self._attach(call)

    def _metric(self, instrument: str, name: str, value: int | float,
                attributes: dict[str, Any], unit: str = '',
                span_context: dict[str, Any] | None = None, *,
                scope: str = 'io.quux.otelc.functions') -> None:
        self.channel.writer.send({'kind': 'metric', 'scope': scope,
            'instrument': instrument, 'name': name, 'value': value, 'unit': unit,
            'attributes': attributes, 'context': span_context})

    def leave(self, key: int, error: BaseException | None) -> None:
        finished = time.perf_counter_ns()
        with self._lock:
            call = self.pending.pop(key, None)
            if call is None or self.closed:
                return
            self._detach(call)
            self.completed += 1
            if call.span is not None:
                if error is not None:
                    kind = exception_type(error)
                    call.span.set_status(trace.StatusCode.ERROR)
                    call.span.add_event('exception', {'exception.type': kind})
                call.span.end()
            if call.collect and self.collection:
                attributes: dict[str, str | int] = {'function': call.name}
                if call.code_id is not None:
                    attributes['otelc.python.code_id'] = call.code_id
                observed = python_tracing.encoded_context(call.span.get_span_context()) if call.span else None
                self._metric('counter', 'otelc.function.calls', 1, attributes,
                             span_context=observed)
                self._metric('histogram', 'otelc.function.duration',
                             (finished - call.started) / 1e9, attributes, 's', observed)
                if error is not None:
                    self._metric('counter', 'otelc.function.exceptions', 1, attributes,
                                 span_context=observed)

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = {'schema_version': 1, 'pid': os.getpid(), 'sdk_pid': self.channel.sdk_pid,
                    'collection': self.collection, 'emission': self.emission,
                    'function_completions': self.completed, 'active_functions': len(self.pending),
                    'runtime_losses': dict(self.losses)}
            if self.lifetimes is not None:
                state['lifetimes'] = self.lifetimes.status()
            return state

    def controls(self, collection: bool, emission: bool) -> dict[str, Any]:
        """Only the control caller waits for the SDK acknowledgement, not probes."""
        if not self._control_lock.acquire(timeout=2.5):
            raise TimeoutError('Python control update is still pending')
        try:
            with self._lock:
                if self.closed:
                    raise RuntimeError('Python runtime is closed')
                try:
                    if self.lifetimes is not None:
                        self.lifetimes.before_controls(collection)
                    if not self.channel.writer.send({'kind': 'controls',
                            'collection': collection, 'emission': emission}):
                        self.reject('control_handoff')
                        raise RuntimeError('Python control handoff failed')
                    self.collection, self.emission = collection, emission
                finally:
                    if self.lifetimes is not None:
                        self.lifetimes.after_controls()
                if self.lifetimes is not None:
                    self.lifetimes.gauges()
            state = self.channel.request('status', timeout=2.5)
            if (state['collection'], state['emission']) != (collection, emission):
                self.reject('control_acknowledgement')
                raise ValueError('Python control acknowledgement differs')
            return {**self.status(), 'effective_emission_off': state['effective_emission_off'],
                    'in_flight_exports': state['in_flight_exports']}
        finally:
            self._control_lock.release()

    def close(self) -> None:
        """Seal honest probe counts; the supervisor owns the SDK drain wait."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            if self.lifetimes is not None:
                self.lifetimes.close()
            spans, errors = self._processor.snapshot() if self._processor else (
                {'sampled_started': 0, 'sampled_ended': 0, 'handed_off': 0}, 0)
            losses = {**self.losses}
            if errors:
                losses['span_processing'] = errors
            checkpoint = {'function_completions': self.completed,
                'active_functions': len(self.pending), 'runtime_losses': losses,
                'span_observation': spans}
            if self.lifetimes is not None:
                checkpoint['lifetimes'] = self.lifetimes.status()
            self.channel.seal(checkpoint)
            if self._provider:
                self._provider.shutdown()
