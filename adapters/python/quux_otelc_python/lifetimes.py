"""Bounded collection observations for ordinary initialisers; no object retention."""
import collections
import itertools
import inspect
import time
import types
import weakref
from dataclasses import dataclass

from opentelemetry.metrics import Observation

from .policy import Selection, source_name


_tokens = itertools.count(1)


@dataclass
class Instance:
    identity: int
    reference: object
    name: str
    started: int
    span: object
    metrics: bool


class Lifetimes:
    def __init__(self, runtime, meter):
        self.runtime = runtime
        self.selection = Selection(runtime.plan['lifetime_matchers'])
        self.frames = {}
        self.initialising = set()
        self.instances = {}
        self.identities = {}
        self.types = {}
        self.losses = collections.Counter()
        self.admitted = self.completed = self.censored = 0
        self.failed_initialisations = 0
        self.closed = False
        self.duration = meter.create_histogram('otelc.lifetime.duration', unit='s')
        meter.create_observable_counter('otelc.lifetime.admitted', callbacks=[lambda _: self.points('admitted')], unit='{instance}')
        meter.create_observable_counter('otelc.lifetime.completed', callbacks=[lambda _: self.points('completed')], unit='{instance}')
        meter.create_observable_gauge('otelc.lifetime.live', callbacks=[lambda _: self.points('live')], unit='{instance}')
        meter.create_observable_counter('otelc.lifetime.dropped_observations', callbacks=[self.loss_points], unit='{observation}')
        meter.create_observable_counter('otelc.lifetime.failed_initialisations', callbacks=[lambda _: [Observation(self.failed_initialisations)]], unit='{instance}')

    def reject(self, reason):
        with self.runtime.lock:
            self.losses[reason] += 1

    def points(self, kind):
        with self.runtime.lock:
            return [Observation(row[kind], {'code.type.name': name, 'otelc.lifetime.boundary': 'collection'})
                    for name, row in self.types.items()]

    def loss_points(self, _):
        with self.runtime.lock:
            return [Observation(count, {'reason': reason}) for reason, count in self.losses.items()]

    @staticmethod
    def receiver(frame, code):
        return frame.f_locals.get(code.co_varnames[0]) if code.co_argcount else None

    def start(self, frame, code, root):
        if self.closed or code.co_name != '__init__':
            return
        source = source_name(code.co_filename, root)
        if source is None:
            return
        if not code.co_argcount or code.co_flags & (inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR):
            guess = source.removesuffix('.py').replace('/', '.') + '.' + code.co_qualname.rsplit('.', 1)[0]
            if self.selection.accepts(guess):
                self.reject('unsupported_initializer')
            return
        value = self.receiver(frame, code)
        cls = type(value)
        # Read raw class dictionaries, never user attribute hooks/descriptors.
        if type(cls) is not type:
            guess = source.removesuffix('.py').replace('/', '.') + '.' + code.co_qualname.rsplit('.', 1)[0]
            if self.selection.accepts(guess):
                self.reject('unsupported_metaclass')
            return
        name = source.removesuffix('.py').replace('/', '.') + '.' + type.__getattribute__(cls, '__qualname__')
        if not self.selection.accepts(name):
            return
        hierarchy = type.__getattribute__(cls, '__mro__')
        mappings = [type.__getattribute__(base, '__dict__') for base in hierarchy]
        initializer = mappings[0].get('__init__')
        # Only the most-derived ordinary initializer admits the instance. Its
        # super calls are ignored; inherited/decorated/C initializers are unqualified.
        if type(initializer) is not types.FunctionType or initializer.__code__ is not code:
            if initializer is None:
                self.reject('unsupported_initializer')
            return
        if any('__del__' in mapping for mapping in mappings):
            self.reject('unsupported_finalizer')
            return
        constructor = next(mapping['__new__'] for mapping in mappings if '__new__' in mapping)
        if constructor is not object.__new__:
            self.reject('unsupported_constructor')
            return
        if not type.__getattribute__(cls, '__weakrefoffset__'):
            self.reject('unsupported_weak_reference')
            return
        identity = id(value)
        with self.runtime.lock:
            existing = self.instances.get(self.identities.get(identity))
            if identity in self.initialising or existing is not None and existing.reference() is value:
                return  # Constructor delegation/reinitialisation is not a new instance.
            if len(self.frames) >= self.runtime.plan['runtime']['max_active_calls']:
                self.reject('constructor_capacity')
                return
            if len(name.encode()) > 1024 or name not in self.types and len(self.types) >= self.runtime.plan['runtime']['max_functions']:
                self.reject('type_capacity')
                return
            self.initialising.add(identity)
            self.frames[id(frame)] = (identity, name)

    def returned(self, frame, code, value, parent):
        with self.runtime.lock:
            entry = self.frames.pop(id(frame), None)
            if entry is None:
                return
            identity, name = entry
            self.initialising.discard(identity)
            if value is not None:
                self.failed_initialisations += 1
                return
            if self.closed or self.runtime.closed:
                return
            if len(self.instances) >= self.runtime.plan['runtime']['max_live_lifetimes']:
                self.reject('lifetime_capacity')
                return
            # A nested/concurrent initializer may have admitted another type
            # since this frame started. Recheck the bound at the commit point.
            if name not in self.types and len(self.types) >= self.runtime.plan['runtime']['max_functions']:
                self.reject('type_capacity')
                return
            obj = self.receiver(frame, code)
            if id(obj) != identity:
                self.reject('receiver_changed')
                return
            token = next(_tokens)
            # The callback captures only our token and SDK owner. Weak refs are
            # never hashed or compared, which would invoke application hash/eq.
            reference = weakref.ref(obj, lambda _: self.collected(token))
            started = time.perf_counter_ns()
            wall = time.time_ns()
            try:
                span = self.runtime.traces.begin_lifetime(name, token, parent, wall)
            except Exception:
                self.reject('invalid')
                return
            metrics = self.runtime.enabled
            self.instances[token] = Instance(identity, reference, name, started, span, metrics)
            self.identities[identity] = token
            self.admitted += 1
            row = self.types.setdefault(name, {'admitted': 0, 'completed': 0, 'live': 0})
            if metrics:
                row['admitted'] += 1
                row['live'] += 1

    def unwound(self, frame):
        with self.runtime.lock:
            entry = self.frames.pop(id(frame), None)
            if entry is not None:
                self.initialising.discard(entry[0])
                self.failed_initialisations += 1

    def collected(self, token):
        # Weak-reference callback exceptions must never escape to app stderr.
        try:
            with self.runtime.lock:
                if self.closed or self.runtime.closed:
                    return
                item = self.instances.pop(token, None)
                if item is None:
                    self.reject('stale_completion')
                    return
                if self.identities.get(item.identity) == token:
                    self.identities.pop(item.identity)
                self.completed += 1
                elapsed = max(0, time.perf_counter_ns() - item.started)
                if item.metrics:
                    row = self.types[item.name]
                    row['completed'] += 1
                    row['live'] -= 1
                    try:
                        self.duration.record(elapsed / 1e9, {'code.type.name': item.name, 'otelc.lifetime.boundary': 'collection'})
                    except Exception:
                        self.reject('invalid')
                self.runtime.traces.finish_lifetime(item.span, elapsed)
        except Exception:
            self.reject('invalid')

    def shutdown(self):
        with self.runtime.lock:
            self.closed = True
            self.censored += len(self.instances)
            if self.instances:
                self.losses['incomplete'] += len(self.instances)
            if self.frames:
                self.losses['incomplete_constructor'] += len(self.frames)
            self.instances.clear()
            self.identities.clear()
            self.frames.clear()
            self.initialising.clear()

    def report(self):
        with self.runtime.lock:
            return {'admitted': self.admitted, 'completed': self.completed,
                    'current_live': self.censored + len(self.instances), 'censored': self.censored,
                    'tracked_instances': len(self.instances), 'active_constructors': len(self.frames),
                    'failed_initialisations': self.failed_initialisations,
                    'losses': dict(self.losses)}
