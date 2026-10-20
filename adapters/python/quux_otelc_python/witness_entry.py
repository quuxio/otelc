"""Private entry bootstrap before imported target code is available."""
from dataclasses import dataclass
import threading
from types import CodeType, FrameType
import weakref

from .witness_callsite import CallSite, bind_site
from .witness_code import CodeBindingError, CodeRegistry
from .witness_owner import Generation, OwnerEvent, OwnerProfile


@dataclass(frozen=True, eq=False)
class EntrySnapshot:
    primary: Generation
    envelope: Generation


class EntryAnchor:
    """Observe primary/envelope generations without preloading application code."""

    def __init__(self, registry: CodeRegistry, primary: CodeType, envelope: CallSite):
        if type(registry) is not CodeRegistry or type(primary) is not CodeType or registry.lookup(primary) is None:
            raise CodeBindingError('unregistered_primary_code')
        if (type(envelope) is not CallSite or type(envelope.caller) is not weakref.ReferenceType
                or type(envelope.callee) is not weakref.ReferenceType):
            raise CodeBindingError('unsupported_entry_site')
        self.site = bind_site(registry, envelope.caller(), envelope.callee(), envelope.call_offset)
        if self.site.caller() is not primary:
            raise CodeBindingError('disconnected_entry_site')
        self.code = weakref.ref(primary)
        self.thread = threading.get_native_id()
        self.lock = threading.RLock()
        self.primary: Generation | None = None
        self.envelope: Generation | None = None
        self.primary_seen = False
        self.envelope_seen = False
        self.closed = False
        self.claimed = False
        self.fault: str | None = None

    def _invalidate(self, reason: str) -> OwnerEvent | None:
        if self.fault is not None:
            return None
        self.fault = reason
        return OwnerEvent('invalidated', outcome=reason)

    def entered(self, frame: FrameType) -> OwnerEvent | None:
        if type(frame) is not FrameType or threading.get_native_id() != self.thread:
            return None
        with self.lock:
            if self.closed or self.fault is not None:
                return None
            if frame.f_code is self.code():
                if self.primary_seen:
                    return self._invalidate('primary_reentered')
                self.primary_seen = True
                self.primary = Generation(id(frame), object())
                return OwnerEvent('primary_enter', activation=self.primary.token)
            if (self.primary is not None and frame.f_back is not None
                    and id(frame.f_back) == self.primary.frame_id and self.site.matches(frame)):
                if self.envelope_seen:
                    return self._invalidate('context_reused')
                self.envelope_seen = True
                self.envelope = Generation(id(frame), object())
                return OwnerEvent('envelope_enter', self.envelope.token, self.envelope.token)
            return None

    def exited(self, frame: FrameType) -> OwnerEvent | None:
        if type(frame) is not FrameType or threading.get_native_id() != self.thread:
            return None
        with self.lock:
            if self.closed:
                return None
            if (self.envelope is not None and id(frame) == self.envelope.frame_id
                    and frame.f_code is self.site.callee()):
                event = OwnerEvent('envelope_exit', self.envelope.token, self.envelope.token)
                self.envelope = None
                return event
            if self.primary is not None and id(frame) == self.primary.frame_id and frame.f_code is self.code():
                event = self._invalidate('unbalanced_entry') if self.envelope is not None else OwnerEvent('primary_exit', activation=self.primary.token)
                self.primary = self.envelope = None
                return event
            return None

    def claim(self, profile: OwnerProfile, frame: FrameType) -> EntrySnapshot:
        """Transfer observed generations only at their actual envelope/rendezvous."""
        if type(profile) is not OwnerProfile or type(frame) is not FrameType or threading.get_native_id() != self.thread:
            raise CodeBindingError('wrong_entry_context')
        with self.lock:
            if self.closed or self.fault is not None or self.primary is None or self.envelope is None:
                raise CodeBindingError('entry_not_live')
            if (profile.primary() is not self.code() or profile.envelope.callee() is not self.site.callee()
                    or profile.envelope.call_offset != self.site.call_offset):
                raise CodeBindingError('wrong_entry_binding')
            envelope = self._envelope_frame(profile, frame)
            primary = envelope.f_back
            if (primary is None or primary.f_code is not self.code()
                    or id(primary) != self.primary.frame_id or not self.site.matches(envelope)):
                raise CodeBindingError('wrong_entry_context')
            snapshot = EntrySnapshot(self.primary, self.envelope)
            self.claimed = self.closed = True
            self.primary = self.envelope = None
            return snapshot

    def _envelope_frame(self, profile: OwnerProfile, frame: FrameType) -> FrameType:
        if frame.f_code is self.site.callee() and id(frame) == self.envelope.frame_id:
            return frame
        caller = frame.f_back
        if (caller is not None and id(caller) == self.envelope.frame_id
                and caller.f_code is self.site.callee() and profile.rendezvous.matches(frame)):
            return caller
        raise CodeBindingError('wrong_entry_context')

    def close(self) -> OwnerEvent | None:
        with self.lock:
            if self.closed:
                return None
            event = self._invalidate('incomplete_entry') if self.primary is not None or self.envelope is not None else None
            self.closed = True
            self.primary = self.envelope = None
            return event
