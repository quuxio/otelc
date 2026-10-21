"""Private single-action protocol state with externally supplied live bindings."""
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

from .witness_wire import (
    MAX_EVENTS, MAX_INTEGER, MAX_REQUESTS, ProtocolError, _identity,
    decode_request, encode_record,
)


PROFILE = 'python-sync-v1'


def fresh_identity() -> str:
    """Issue a nonzero process-local protocol identity, independent of names/PIDs."""
    while True:
        value = secrets.token_hex(16)
        if int(value, 16):
            return value


@dataclass(frozen=True, eq=False)
class Probe:
    """Private binding handle; a matching digest alone is not an entry probe."""
    digest: str

    def __post_init__(self) -> None:
        _identity(self.digest, 64, nonzero=False)


@dataclass(frozen=True)
class Bindings:
    build_id: str
    selection_id: str
    rendezvous: Probe
    wrapper: Probe
    target: Probe
    call_site: object

    def __post_init__(self) -> None:
        _identity(self.build_id, 64, nonzero=False)
        _identity(self.selection_id, 64, nonzero=False)
        probes = (self.rendezvous, self.wrapper, self.target)
        if (any(type(probe) is not Probe for probe in probes)
                or type(self.call_site) is not object
                or len({probe.digest for probe in probes}) != 3):
            raise ProtocolError('wrong_binding')


@dataclass(frozen=True)
class Health:
    inflight: int = 0
    pending: int = 0
    losses: int = 0
    export_finished: bool = False

    def __post_init__(self) -> None:
        values = (self.inflight, self.pending, self.losses)
        if any(type(value) is not int or not 0 <= value <= MAX_INTEGER for value in values):
            raise ProtocolError('invalid_sequence')
        if type(self.export_finished) is not bool:
            raise ProtocolError('malformed_record')

    def record(self, extra_losses: int = 0) -> dict[str, Any]:
        return {'inflight': self.inflight, 'pending': self.pending,
                'losses': min(MAX_INTEGER, self.losses + extra_losses),
                'export_finished': self.export_finished}

    @property
    def clean(self) -> bool:
        return not (self.inflight or self.pending or self.losses) and self.export_finished


class WitnessState:
    """Serialise one private scope using observed compiler/context bindings.

    The private launcher supplies actual handles through its bootstrap and SDK
    bridge. Tests which construct handles demonstrate state behaviour only;
    neither the kernel nor those model tests qualify a public witness profile.
    """

    def __init__(self, bindings: Bindings, *, available: bool = False) -> None:
        if type(bindings) is not Bindings or type(available) is not bool:
            raise ProtocolError('wrong_binding')
        self.bindings = bindings
        self.available = available
        self.runtime_id = fresh_identity()
        self.lock = threading.RLock()
        self.phase = 'idle'
        self.fault: str | None = None
        self.fault_count = 0
        self.requests_closed = False
        self.request_count = 0
        self.request_ids: set[str] = set()
        self.events: deque[bytes] = deque()
        self.event_sequence = 0
        self.owner: object | None = None
        self.owner_live = False
        self.context_id: str | None = None
        self.ready_sequence = 0
        self.scope_id: str | None = None
        self.claim: object | None = None
        self.trace_id: str | None = None
        self.span_id: str | None = None
        self.target_entries = 0
        self.root_exited = False
        self.outcome: str | None = None
        self.terminal_emitted = False

    def _append(self, event: str, payload: dict[str, Any]) -> None:
        self.event_sequence += 1
        record = {'version': 1, 'kind': 'event', 'event': event,
                  'event_seq': self.event_sequence, 'runtime_id': self.runtime_id, **payload}
        self.events.append(encode_record(record))

    def _emit(self, event: str, **payload: Any) -> bool:
        # Keep two slots for permanent invalidation and final runtime health.
        if len(self.events) >= MAX_EVENTS - 2 or self.event_sequence >= MAX_INTEGER - 2:
            self._invalidate('event_capacity')
            return False
        self._append(event, payload)
        return True

    def _invalidate(self, reason: str) -> None:
        reason = ProtocolError(reason).code
        self.fault_count = min(MAX_INTEGER, self.fault_count + 1)
        self.requests_closed = True
        self.phase = 'invalidated'
        if self.fault is None:
            self.fault = reason
            self._append('invalidated', {'scope_id': self.scope_id, 'reason': reason})

    def drain(self) -> tuple[bytes, ...]:
        """Transfer owned immutable frames without dropping earlier sequence entries."""
        with self.lock:
            result = tuple(self.events)
            self.events.clear()
            return result

    def invalidate(self, reason: str = 'incomplete') -> None:
        """Record a permanent runtime refusal through the same bounded stream."""
        with self.lock:
            self._invalidate(reason)

    def activate(self, bindings: Bindings) -> bool:
        """Replace declared handles with actual compiler bindings before readiness."""
        with self.lock:
            expected = self.bindings
            if (self.available or self.phase != 'idle' or self.fault is not None
                    or type(bindings) is not Bindings
                    or bindings.build_id != expected.build_id or bindings.selection_id != expected.selection_id
                    or any(getattr(bindings, role).digest != getattr(expected, role).digest
                           for role in ('rendezvous', 'wrapper', 'target'))):
                self._invalidate('wrong_binding')
                return False
            self.bindings = bindings
            self.available = True
            return True

    def rendezvous(self, probe: Probe, context: object) -> bool:
        with self.lock:
            if not self.available or probe is not self.bindings.rendezvous:
                return False
            if type(context) is not object:
                self._invalidate('wrong_context')
                return False
            if self.phase != 'idle':
                if context is self.owner:
                    self._invalidate('invalid_phase')
                return False
            self.owner, self.owner_live = context, True
            self.context_id = fresh_identity()
            self.ready_sequence = 1
            self.phase = 'ready'
            return self._emit('ready', context_id=self.context_id, ready_seq=self.ready_sequence,
                              rendezvous_probe_id=probe.digest, build_id=self.bindings.build_id,
                              selection_id=self.bindings.selection_id)

    def _response(self, request: dict[str, Any], **fields: Any) -> bytes:
        return encode_record({'version': 1, 'kind': 'response', 'op': request['op'],
                              'request_id': request['request_id'], 'runtime_id': self.runtime_id, **fields})

    def _reject(self, request: dict[str, Any], reason: str) -> bytes:
        self._invalidate(reason)
        return self._response(request, ok=False, error=reason)

    def handle(self, frame: bytes) -> bytes | None:
        """Malformed/duplicate identities never receive a fabricated echo reply."""
        with self.lock:
            if self.requests_closed:
                return None
            if self.request_count >= MAX_REQUESTS:
                self._invalidate('request_capacity')
                return None
            self.request_count += 1
            try:
                request = decode_request(frame)
            except ProtocolError as error:
                self._invalidate(error.code)
                return None
            if request['request_id'] in self.request_ids:
                self._invalidate('duplicate_request')
                return None
            self.request_ids.add(request['request_id'])
            if request['op'] == 'capabilities':
                return self._capabilities(request)
            if request['runtime_id'] != self.runtime_id:
                return self._reject(request, 'wrong_runtime')
            if request['op'] == 'cancel':
                return self._cancel(request)
            return self._arm(request)

    def _capabilities(self, request: dict[str, Any]) -> bytes:
        profile = request['profile']
        if not self.available or profile != PROFILE:
            return self._response(request, ok=True, profile=profile, available=False,
                                  reason='disabled' if profile == PROFILE else 'unsupported_profile')
        return self._response(request, ok=True, profile=profile, available=True,
                              build_id=self.bindings.build_id, selection_id=self.bindings.selection_id)

    def _arm(self, request: dict[str, Any]) -> bytes:
        if not self.available:
            return self._reject(request, 'unsupported_profile')
        if self.scope_id is not None:
            return self._reject(request, 'scope_reused')
        if self.phase != 'ready' or not self.owner_live:
            return self._reject(request, 'invalid_phase')
        if request['context_id'] != self.context_id:
            return self._reject(request, 'wrong_context')
        if request['ready_seq'] != self.ready_sequence:
            return self._reject(request, 'stale_readiness')
        expected = {'build_id': self.bindings.build_id, 'selection_id': self.bindings.selection_id,
                    'wrapper_probe_id': self.bindings.wrapper.digest, 'target_probe_id': self.bindings.target.digest}
        if any(request[key] != value for key, value in expected.items()):
            return self._reject(request, 'wrong_binding')
        self.scope_id = request['scope_id']
        self.phase = 'armed'
        echo = {key: value for key, value in request.items()
                if key not in ('version', 'kind', 'request_id', 'op', 'runtime_id')}
        return self._response(request, ok=True, **echo)

    def _cancel(self, request: dict[str, Any]) -> bytes:
        if self.scope_id is None or request['scope_id'] != self.scope_id:
            return self._reject(request, 'wrong_binding')
        consumed = self.trace_id is not None
        self._invalidate('cancelled')
        return self._response(request, ok=True, scope_id=self.scope_id, consumed=consumed)

    def claim_wrapper(self, probe: Probe, context: object, call_site: object) -> object | None:
        """Only actual backend handles can reserve the single intended invocation."""
        with self.lock:
            if (not self.available or self.owner is None
                    or probe is not self.bindings.wrapper or context is not self.owner
                    or call_site is not self.bindings.call_site):
                return None
            if self.phase != 'armed' or not self.owner_live or self.fault is not None:
                self._invalidate('late_activity' if self.root_exited else 'invalid_phase')
                return None
            self.claim = object()
            self.phase = 'claimed'
            return self.claim

    def bind_root(self, claim: object, trace_id: int, span_id: int, *, sampled: bool) -> bool:
        with self.lock:
            if claim is not self.claim or self.claim is None:
                return False
            if self.phase != 'claimed':
                self._invalidate('invalid_phase')
                return False
            if (type(trace_id) is not int or not 0 < trace_id < 1 << 128
                    or type(span_id) is not int or not 0 < span_id < 1 << 64):
                self._invalidate('invalid_identity')
                return False
            if sampled is not True:
                self._invalidate('incomplete')
                return False
            self.trace_id, self.span_id = f'{trace_id:032x}', f'{span_id:016x}'
            self.phase = 'consumed'
            return self._emit('consumed', **self._root(), context_id=self.context_id,
                              ready_seq=self.ready_sequence, wrapper_probe_id=self.bindings.wrapper.digest,
                              target_probe_id=self.bindings.target.digest)

    def _root(self) -> dict[str, str | None]:
        return {'scope_id': self.scope_id, 'root_trace_id': self.trace_id, 'root_span_id': self.span_id}

    def activity(self, claim: object, context: object, probe: Probe) -> bool:
        with self.lock:
            if claim is not self.claim or self.claim is None or context is not self.owner:
                return False
            if self.phase != 'consumed' or not self.owner_live:
                self._invalidate('late_activity')
                return False
            if probe is self.bindings.target:
                self.target_entries = min(MAX_INTEGER, self.target_entries + 1)
            return True

    def root_exit(self, claim: object, outcome: str) -> bool:
        with self.lock:
            if claim is not self.claim or self.claim is None or self.trace_id is None:
                return False
            if self.root_exited:
                self._invalidate('invalid_phase')
                return False
            if type(outcome) is not str or outcome not in ('return', 'exception', 'cancelled', 'incomplete'):
                self._invalidate('malformed_record')
                return False
            self.root_exited, self.outcome = True, outcome
            if self.fault is None:
                self.phase = 'root_exited'
            return self._emit('root_exit', **self._root(), outcome=outcome)

    def provisional_end(self, health: Health) -> bool:
        with self.lock:
            if type(health) is not Health or self.phase != 'root_exited':
                self._invalidate('invalid_phase')
                return False
            if not self.target_entries:
                self._invalidate('wrong_binding')
                return False
            self.phase = 'provisional'
            result = self._emit('end', **self._root(), outcome=self.outcome,
                                provisional=True, health=health.record())
            if health.inflight or health.pending or health.losses:
                self._invalidate('incomplete')
            return result

    def retire_context(self, context: object) -> None:
        with self.lock:
            if context is self.owner and self.owner is not None:
                self.owner_live = False
                if self.phase not in ('provisional', 'invalidated', 'finished'):
                    self._invalidate('incomplete')

    def finish(self, health: Health) -> bool | None:
        """Emit final producer health once; the consumer still needs EOF/process exit."""
        with self.lock:
            if self.terminal_emitted:
                return None
            if type(health) is not Health:
                self._invalidate('incomplete')
                health = Health(losses=1)
            complete = (self.phase == 'provisional' and self.fault is None and not self.owner_live
                        and self.outcome in ('return', 'exception') and health.clean)
            if not complete and self.fault is None:
                self._invalidate('incomplete')
            self.requests_closed = True
            self.terminal_emitted = True
            self.phase = 'finished'
            self._append('runtime_end', {'scope_id': self.scope_id, 'complete': complete,
                                         'health': health.record(self.fault_count)})
            return complete
