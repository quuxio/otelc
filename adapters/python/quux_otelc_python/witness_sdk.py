"""Private monitoring/SDK integration; no public manifest or control endpoint yet."""
from types import CodeType, FrameType

from .witness_code import CodeBindingError, CodeRegistry
from .witness_owner import ExecutionOwner, OwnerEvent, OwnerProfile
from .witness_state import WitnessState


class WitnessSDK:
    """Bind an already qualified original-code owner to actual admitted SDK spans."""

    def __init__(self, runtime, registry: CodeRegistry, profile: OwnerProfile):
        state = runtime.witness_state
        if type(state) is not WitnessState or type(profile) is not OwnerProfile:
            raise CodeBindingError('missing_witness_state')
        bindings = state.bindings
        if (type(registry) is not CodeRegistry
                or registry.lookup(profile.rendezvous.callee()) is not bindings.rendezvous
                or registry.lookup(profile.wrapper.callee()) is not bindings.wrapper
                or registry.lookup(profile.target.callee()) is not bindings.target):
            raise CodeBindingError('mismatched_witness_probes')
        self.runtime, self.state = runtime, state
        self.owner = ExecutionOwner(profile)
        self.claim = None

    def watches(self, code: CodeType) -> bool:
        profile = self.owner.profile
        return any(code is candidate for candidate in (
            profile.primary(), profile.envelope.callee(), profile.rendezvous.callee(),
            profile.wrapper.callee(), profile.target.callee()))

    def entered(self, frame: FrameType) -> OwnerEvent | None:
        event = self.owner.entered(frame)
        if event is None:
            return None
        if event.kind == 'invalidated':
            self.state.invalidate()
        elif event.kind == 'rendezvous':
            self.state.rendezvous(self.state.bindings.rendezvous, event.context)
        elif event.kind == 'wrapper_enter':
            self.claim = self.state.claim_wrapper(self.state.bindings.wrapper, event.context,
                                                  self.state.bindings.call_site)
        return event

    def admitted(self, frame: FrameType, event: OwnerEvent | None) -> None:
        if event is None or event.kind not in ('wrapper_enter', 'target_enter') or self.claim is None:
            return
        state = self.state
        with self.runtime.lock:
            pending = self.runtime.pending.get(id(frame))
            identity = pending.trace if pending is not None else None
            if event.kind == 'wrapper_enter':
                context = identity.context if identity is not None else None
                if context is None or not state.bind_root(
                        self.claim, context.trace_id, context.span_id, sampled=identity.sampled):
                    state.invalidate()
                    return
                probe = state.bindings.wrapper
            else:
                if not state.activity(self.claim, event.context, state.bindings.target):
                    return
                probe = state.bindings.target
            if not self.runtime.traces.bind_witness(identity, state.scope_id, probe.digest):
                state.invalidate()

    def exited(self, frame: FrameType, outcome: str) -> None:
        event = self.owner.exited(frame, outcome)
        if event is None:
            return
        if event.kind == 'invalidated':
            self.state.invalidate()
        elif event.kind == 'wrapper_exit' and self.claim is not None:
            if self.state.root_exit(self.claim, outcome):
                self.state.provisional_end(self.runtime.witness_health())
        elif event.kind == 'envelope_exit':
            self.state.retire_context(event.context)

    def close(self) -> None:
        if self.owner.close() is not None:
            self.state.invalidate()
