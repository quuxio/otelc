"""Bounded weak observed-GC/resource lifetimes driven by common configuration."""

from __future__ import annotations

import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from types import CodeType, FrameType
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.context import Context

from otelc.config import Policy
from otelc import python_objects as objects

if TYPE_CHECKING:
    from otelc.python_runtime import PythonRuntime

MAX_TYPES = 4096
MAX_FRAMES = 65536
MAX_TOKEN = 2 ** 63 - 1


@dataclass
class ClassInfo:
    reference: weakref.ReferenceType[type]
    name: str
    initializer: CodeType | None
    releases: tuple[CodeType, ...]


@dataclass
class Construction:
    type_id: int
    failed: bool = False
    pending: Initializer | None = None


@dataclass
class Initializer:
    identity: int
    reference: weakref.ReferenceType | None
    metadata: ClassInfo
    call: Construction | None


@dataclass
class Instance:
    identity: int
    reference: weakref.ReferenceType
    metadata: ClassInfo
    started: int
    epoch: int
    parent: trace.SpanContext
    metrics: bool
    released: bool = False


class Lifetimes:
    """Never hash/compare instances or retain their frames, classes or finalisers."""

    def __init__(self, policy: Policy, root: Path, runtime: PythonRuntime) -> None:
        self.runtime = runtime
        self.root = root.resolve(strict=True)
        self.selection = policy.selection('lifetimes')
        self.sources = policy.selection('sources')
        self.maximum = policy.data['lifetimes']['max_live']
        self.release_names = tuple(policy.data['lifetimes']['release_methods'])
        self.tracer = runtime._provider.get_tracer('io.quux.otelc.lifetimes') if runtime._provider else None
        self.types: dict[int, ClassInfo] = {}
        self.codes: dict[int, tuple[CodeType, int]] = {}
        self.calls: dict[tuple[int, int], Construction] = {}
        self.initializers: dict[int, Initializer] = {}
        self.initializing: set[int] = set()
        self.releases: dict[int, tuple[int, int]] = {}
        self.instances: dict[int, Instance] = {}
        self.identities: dict[int, int] = {}
        self.losses: dict[str, int] = {}
        self.admitted = self.gc_completed = self.resource_completed = 0
        self.failed_initialisations = self.failed_releases = self.open_resources = 0
        self.censored = self._next = 0
        self.closed = False

    def reject(self, reason: str) -> None:
        with self.runtime._lock:
            self.losses[reason] = self.losses.get(reason, 0) + 1
            self._metric('counter', 'otelc.lifetime.lost', 1, 'all', reason)

    def _metric(self, instrument: str, name: str, value: int | float,
                kind: str, boundary: str, unit: str = '') -> None:
        if self.runtime.collection:
            self.runtime._metric(instrument, name, value,
                {'type': kind, 'boundary': boundary}, unit, scope='io.quux.otelc.lifetimes')

    def gauges(self) -> None:
        self._metric('gauge', 'otelc.lifetime.live', self.censored if self.closed else len(self.instances),
                     'all', 'observed_gc')
        self._metric('gauge', 'otelc.lifetime.live', self.open_resources, 'all', 'resource')

    def _remove_type(self, identity: int) -> None:
        with self.runtime._lock:
            entry = self.types.pop(identity, None)
            if entry is None:
                return
            codes = (() if entry.initializer is None else (entry.initializer,)) + entry.releases
            for code in {id(code): code for code in codes}.values():
                saved, count = self.codes[id(code)]
                if count == 1:
                    del self.codes[id(code)]
                else:
                    self.codes[id(code)] = saved, count - 1

    def metadata(self, cls: type) -> ClassInfo | None:
        entry = self.types.get(id(cls))
        if entry is not None and entry.reference() is cls:
            return entry
        initializer = objects.function_code(objects.resolved(cls, '__init__'))
        source = objects.source_name(cls, self.root, initializer)
        if source is None or not self.sources.accepts(source[0]) or not self.selection.accepts(source[1]):
            return None
        if len(source[1].encode()) > 1024 or len(self.types) >= MAX_TYPES:
            self.reject('type_capacity')
            return None
        releases = []
        for name in self.release_names:
            method = objects.resolved(cls, name)
            code = objects.function_code(method)
            if code is not None:
                releases.append(code)
            elif method is not None:
                self.reject('unsupported_release')
        identity = id(cls)
        reference = weakref.ref(cls, lambda _: self._remove_type(identity))
        entry = ClassInfo(reference, source[1], initializer, tuple(releases))
        self.types[identity] = entry
        codes = (() if initializer is None else (initializer,)) + entry.releases
        for code in {id(code): code for code in codes}.values():
            self.codes[id(code)] = code, self.codes.get(id(code), (code, 0))[1] + 1
        return entry

    def class_call(self, frame: FrameType, offset: int, value: object) -> None:
        if self.closed or not objects.is_class(value):
            return
        with self.runtime._lock:
            metadata = self.metadata(value)
            if metadata is None:
                return
            if objects.resolved(type(value), '__call__') is not type.__dict__['__call__']:
                self.reject('unsupported_metaclass_call')
                return
            if len(self.calls) >= MAX_FRAMES:
                self.reject('constructor_capacity')
                return
            self.calls[id(frame), offset] = Construction(id(value))
            if metadata.initializer is None:
                self.reject('unsupported_initializer')

    def class_finished(self, frame: FrameType, offset: int, *, failed: bool) -> None:
        with self.runtime._lock:
            call = self.calls.pop((id(frame), offset), None)
            if call is not None:
                if failed and not call.failed:
                    self.failed_initialisations += 1
                elif not failed and call.pending is not None:
                    self._admit(call.pending)

    def start(self, frame: FrameType) -> None:
        if self.closed:
            return
        code = frame.f_code
        if code.co_name != '__init__' and id(code) not in self.codes:
            return
        value = objects.receiver(frame)
        with self.runtime._lock:
            metadata = self.metadata(type(value))
            if metadata is None:
                return
            if metadata.initializer is code:
                self._start_initializer(frame, value, metadata)
            elif any(code is item for item in metadata.releases):
                self._start_release(frame, value)

    def _start_initializer(self, frame: FrameType, value: object, metadata: ClassInfo) -> None:
        identity = id(value)
        existing = self.instances.get(self.identities.get(identity))
        if (identity in self.initializing or existing is not None and existing.reference() is value
                or not self.runtime.collection and self.tracer is None):
            return
        if len(self.initializers) >= MAX_FRAMES:
            self.reject('constructor_capacity')
            return
        caller = frame.f_back
        call = self.calls.get((id(caller), caller.f_lasti)) if caller is not None else None
        if call is None or call.type_id != id(type(value)):
            return
        try:
            reference = weakref.ref(value)
        except TypeError:
            self.reject('unsupported_weak_reference')
            reference = None
        self.initializing.add(identity)
        self.initializers[id(frame)] = Initializer(identity, reference, metadata, call)

    def _start_release(self, frame: FrameType, value: object) -> None:
        token = self.identities.get(id(value))
        item = self.instances.get(token)
        if item is None or item.reference() is not value:
            return
        ancestor = frame.f_back
        for _ in range(4096):
            if ancestor is None:
                break
            if self.releases.get(id(ancestor)) == (item.identity, token):
                return
            ancestor = ancestor.f_back
        else:
            self.reject('release_depth')
            return
        if len(self.releases) >= MAX_FRAMES:
            self.reject('release_capacity')
            return
        self.releases[id(frame)] = item.identity, token

    def finish(self, frame: FrameType, value: object, error: BaseException | None) -> None:
        with self.runtime._lock:
            initializer = self.initializers.pop(id(frame), None)
            if initializer is not None:
                self.initializing.discard(initializer.identity)
                if error is not None or value is not None:
                    self.failed_initialisations += 1
                    if initializer.call is not None:
                        initializer.call.failed = True
                elif initializer.reference is not None:
                    obj = objects.receiver(frame)
                    if initializer.reference() is not obj or id(obj) != initializer.identity:
                        self.reject('receiver_changed')
                    else:
                        initializer.call.pending = initializer
                return
            release = self.releases.pop(id(frame), None)
            if release is not None:
                self._release(frame, release, value, error)

    def _admit(self, initializer: Initializer) -> None:
        obj = initializer.reference()
        if obj is None:
            self.reject('unobserved_constructor_result')
            return
        if len(self.instances) >= self.maximum or self._next >= MAX_TOKEN:
            self.reject('lifetime_capacity')
            return
        self._next += 1
        token = self._next
        reference = weakref.ref(obj, lambda _: self._collected(token))
        item = Instance(id(obj), reference, initializer.metadata, time.perf_counter_ns(),
            time.time_ns(), trace.get_current_span().get_span_context(), self.runtime.collection)
        self.instances[token] = item
        self.identities[item.identity] = token
        self.admitted += 1
        if item.metadata.releases:
            self.open_resources += 1
        if item.metrics:
            self._metric('counter', 'otelc.lifetime.admitted', 1, item.metadata.name, 'observed_gc')
            if item.metadata.releases:
                self._metric('counter', 'otelc.lifetime.admitted', 1, item.metadata.name, 'resource')
        self.gauges()

    def _release(self, frame: FrameType, release: tuple[int, int], value: object,
                 error: BaseException | None) -> None:
        identity, token = release
        item = self.instances.get(token)
        if item is None:
            return
        obj = objects.receiver(frame)
        if item.reference() is not obj or id(obj) != identity:
            self.reject('receiver_changed')
            return
        if error is not None:
            self.failed_releases += 1
        elif objects.deferred(value):
            self.reject('deferred_release_result')
        elif not item.released:
            item.released = True
            self.open_resources -= 1
            self.resource_completed += 1
            self._complete(item, token, 'resource')
            self.gauges()

    def _collected(self, token: int) -> None:
        try:
            with self.runtime._lock:
                if self.closed:
                    return
                item = self.instances.pop(token, None)
                if item is None:
                    self.reject('stale_completion')
                    return
                if self.identities.get(item.identity) == token:
                    del self.identities[item.identity]
                self.gc_completed += 1
                if item.metadata.releases and not item.released:
                    self.open_resources -= 1
                    if item.metrics:
                        self._metric('counter', 'otelc.lifetime.incomplete', 1, item.metadata.name, 'resource')
                self._complete(item, token, 'observed_gc')
                self.gauges()
        except Exception:
            self.reject('gc_observation')

    def _complete(self, item: Instance, token: int, boundary: str) -> None:
        elapsed = max(0, time.perf_counter_ns() - item.started)
        if item.metrics:
            self._metric('counter', 'otelc.lifetime.completed', 1, item.metadata.name, boundary)
            self._metric('histogram', 'otelc.lifetime.duration', elapsed / 1e9,
                         item.metadata.name, boundary, 's')
        if self.tracer is not None:
            links = [trace.Link(item.parent)] if item.parent.is_valid else []
            span = self.tracer.start_span(item.metadata.name + ' lifetime', context=Context(),
                start_time=item.epoch, links=links, attributes={'type': item.metadata.name,
                    'boundary': boundary, 'otelc.lifetime.instance': str(token)})
            span.end(end_time=item.epoch + elapsed)

    def status(self) -> dict[str, Any]:
        with self.runtime._lock:
            return {'admitted': self.admitted, 'gc_completed': self.gc_completed,
                'resource_completed': self.resource_completed,
                'current_live': self.censored if self.closed else len(self.instances),
                'open_resources': self.open_resources, 'tracked_instances': len(self.instances),
                'tracked_constructors': len(self.types), 'failed_initialisations': self.failed_initialisations,
                'failed_releases': self.failed_releases, 'censored': self.censored,
                'lost': sum(self.losses.values()), 'losses': dict(self.losses)}

    def close(self) -> None:
        with self.runtime._lock:
            if self.closed:
                return
            self.closed = True
            self.censored = len(self.instances)
            if self.initializers or self.calls:
                self.reject('incomplete_construction')
            if self.releases:
                self.reject('incomplete_release')
            self.instances.clear()
            self.identities.clear()
            self.initializers.clear()
            self.initializing.clear()
            self.releases.clear()
            self.calls.clear()
            self.types.clear()
            self.codes.clear()
