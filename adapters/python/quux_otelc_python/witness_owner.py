"""Private synchronous execution ownership; no SDK/control integration yet."""
from dataclasses import dataclass
import threading
from types import CodeType, FrameType
import weakref

from .witness_callsite import CallSite, bind_site
from .witness_code import CodeBindingError, CodeRegistry


MAX_TARGETS = 4096


@dataclass(frozen=True, eq=False)
class Generation:
    frame_id: int
    token: object


@dataclass(frozen=True, eq=False)
class OwnerEvent:
    kind: str
    context: object | None = None
    activation: object | None = None
    outcome: str | None = None


class OwnerProfile:
    """Validate the complete primary/envelope/wrapper/target caller chain."""

    def __init__(self, registry: CodeRegistry, primary: CodeType, envelope: CallSite,
                 rendezvous: CallSite, wrapper: CallSite, target: CallSite):
        if type(registry) is not CodeRegistry or type(primary) is not CodeType or registry.lookup(primary) is None:
            raise CodeBindingError('unregistered_primary_code')
        self.primary = weakref.ref(primary)
        self.envelope, self.rendezvous, self.wrapper, self.target = (
            self._normalized(registry, site) for site in (envelope, rendezvous, wrapper, target))
        envelope_code = self.envelope.callee()
        if (self.envelope.caller() is not primary or self.rendezvous.caller() is not envelope_code
                or self.wrapper.caller() is not envelope_code
                or self.target.caller() is not self.wrapper.callee()):
            raise CodeBindingError('disconnected_owner_profile')
        codes = (primary, envelope_code, self.rendezvous.callee(), self.wrapper.callee(), self.target.callee())
        if len({id(code) for code in codes}) != 5:
            raise CodeBindingError('overlapping_owner_roles')

    @staticmethod
    def _normalized(registry: CodeRegistry, site: CallSite) -> CallSite:
        if (type(site) is not CallSite or type(site.caller) is not weakref.ReferenceType
                or type(site.callee) is not weakref.ReferenceType):
            raise CodeBindingError('unsupported_owner_site')
        # Rebuild observation metadata from registered original code. A manually
        # constructed dataclass cannot smuggle a different observation offset.
        return bind_site(registry, site.caller(), site.callee(), site.call_offset)


class ExecutionOwner:
    """Track native-thread and live frame generations without retaining frames."""

    def __init__(self, profile: OwnerProfile, max_targets: int = 64):
        if type(profile) is not OwnerProfile or type(max_targets) is not int or not 1 <= max_targets <= MAX_TARGETS:
            raise CodeBindingError('unsupported_execution_owner')
        self.profile = profile
        self.max_targets = max_targets
        self.thread = threading.get_native_id()
        self.lock = threading.RLock()
        self.primary: Generation | None = None
        self.envelope: Generation | None = None
        self.wrapper: Generation | None = None
        self.targets: dict[int, Generation] = {}
        self.primary_seen = False
        self.envelope_seen = False
        self.fault: str | None = None
        self.closed = False

    @property
    def inflight(self) -> int:
        with self.lock:
            return int(self.wrapper is not None) + len(self.targets)

    def _invalidate(self, reason: str) -> OwnerEvent | None:
        if self.fault is not None:
            return None
        self.fault = reason
        return OwnerEvent('invalidated', self.envelope.token if self.envelope else None, outcome=reason)

    def _event(self, kind: str, generation: Generation, outcome: str | None = None) -> OwnerEvent:
        return OwnerEvent(kind, self.envelope.token if self.envelope else None, generation.token, outcome)

    @staticmethod
    def _direct(frame: FrameType, parent: Generation | None, site: CallSite) -> bool:
        return parent is not None and frame.f_back is not None and id(frame.f_back) == parent.frame_id and site.matches(frame)

    def entered(self, frame: FrameType) -> OwnerEvent | None:
        if type(frame) is not FrameType or threading.get_native_id() != self.thread:
            return None
        with self.lock:
            if self.closed or self.fault is not None:
                return None
            if frame.f_code is self.profile.primary():
                if self.primary_seen:
                    return self._invalidate('primary_reentered')
                self.primary_seen = True
                self.primary = Generation(id(frame), object())
                return self._event('primary_enter', self.primary)
            if self.primary is None:
                return None
            return self._enter_scope(frame)

    def _enter_scope(self, frame: FrameType) -> OwnerEvent | None:
        if self._direct(frame, self.primary, self.profile.envelope):
            if self.envelope_seen:
                return self._invalidate('context_reused')
            self.envelope_seen = True
            self.envelope = Generation(id(frame), object())
            return self._event('envelope_enter', self.envelope)
        if self._direct(frame, self.envelope, self.profile.rendezvous):
            return self._event('rendezvous', self.envelope)
        if self._direct(frame, self.envelope, self.profile.wrapper):
            if self.wrapper is not None:
                return self._invalidate('wrapper_reentered')
            self.wrapper = Generation(id(frame), object())
            return self._event('wrapper_enter', self.wrapper)
        if frame.f_code is self.profile.target.callee() and self.wrapper is not None:
            return self._enter_target(frame)
        return None

    def _enter_target(self, frame: FrameType) -> OwnerEvent | None:
        caller = frame.f_back
        recursive = (caller is not None and caller.f_code is self.profile.target.callee()
                     and id(caller) in self.targets)
        if not recursive and not self._direct(frame, self.wrapper, self.profile.target):
            return None
        if len(self.targets) >= self.max_targets or id(frame) in self.targets:
            return self._invalidate('target_activation_capacity')
        generation = Generation(id(frame), object())
        self.targets[id(frame)] = generation
        return self._event('target_enter', generation)

    def exited(self, frame: FrameType, outcome: str) -> OwnerEvent | None:
        if type(frame) is not FrameType or threading.get_native_id() != self.thread:
            return None
        with self.lock:
            if self.closed:
                return None
            if type(outcome) is not str or outcome not in ('return', 'exception'):
                return self._invalidate('invalid_exit_outcome')
            generation = self.targets.pop(id(frame), None) if frame.f_code is self.profile.target.callee() else None
            if generation is not None:
                return self._event('target_exit', generation, outcome)
            return self._exit_scope(frame, outcome)

    def _exit_scope(self, frame: FrameType, outcome: str) -> OwnerEvent | None:
        code, key = frame.f_code, id(frame)
        for role, expected in (('wrapper', self.profile.wrapper.callee()),
                               ('envelope', self.profile.envelope.callee()),
                               ('primary', self.profile.primary())):
            generation = getattr(self, role)
            if generation is None or generation.frame_id != key or code is not expected:
                continue
            event = self._event(role + '_exit', generation, outcome)
            if (self.targets or role != 'wrapper' and self.wrapper is not None
                    or role == 'primary' and self.envelope is not None):
                event = self._invalidate('unbalanced_execution')
            self._retire(role)
            return event
        return None

    def _retire(self, role: str) -> None:
        if role == 'primary':
            self.envelope = None
        if role != 'wrapper':
            self.wrapper = None
        self.targets.clear()
        setattr(self, role, None)

    def close(self) -> OwnerEvent | None:
        with self.lock:
            if self.closed:
                return None
            event = self._invalidate('incomplete_execution') if self.primary is not None or self.envelope is not None or self.inflight else None
            self.closed = True
            self.primary = self.envelope = self.wrapper = None
            self.targets.clear()
            return event
