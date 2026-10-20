"""Private CPython CALL identity checks; live envelope ownership is separate."""
from dataclasses import dataclass
import inspect
import opcode
import sys
from types import CodeType, FrameType
import weakref

from .witness_code import CodeBindingError, CodeRegistry


@dataclass(frozen=True, eq=False)
class CallSite:
    """Weak exact caller/callee identities and the pinned caller observation offset."""
    caller: weakref.ReferenceType
    callee: weakref.ReferenceType
    call_offset: int
    observation_offset: int
    caller_probe_id: str
    callee_probe_id: str
    profile: str

    def matches(self, callee_frame: FrameType) -> bool:
        if type(callee_frame) is not FrameType:
            return False
        caller = callee_frame.f_back
        return (callee_frame.f_code is self.callee() and caller is not None
                and caller.f_code is self.caller() and caller.f_lasti == self.observation_offset)

    def record(self) -> dict:
        """Describe this compiler site; this alone is not a frozen action manifest."""
        return {'profile': self.profile, 'caller_probe_id': self.caller_probe_id,
                'callee_probe_id': self.callee_probe_id, 'call_offset': self.call_offset,
                'observation_offset': self.observation_offset}


def bind_site(registry: CodeRegistry, caller: CodeType, callee: CodeType, call_offset: int) -> CallSite:
    """Refuse unknown objects/versions/sites without disassembling application values."""
    if (sys.implementation.name != 'cpython' or sys.version_info[:2] not in ((3, 12), (3, 14))
            or type(registry) is not CodeRegistry or type(caller) is not CodeType
            or type(callee) is not CodeType):
        raise CodeBindingError('unsupported_callsite_profile')
    caller_probe, callee_probe = registry.lookup(caller), registry.lookup(callee)
    if caller_probe is None or callee_probe is None:
        raise CodeBindingError('unregistered_callsite_code')
    # Ordinary synchronous CALL only. Generator/coroutine and CALL_KW profiles
    # need separate caller-offset and lifecycle qualification.
    if (caller.co_flags | callee.co_flags) & (inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR):
        raise CodeBindingError('unsupported_callsite_shape')
    code = caller.co_code
    if (type(call_offset) is not int or call_offset < 0 or call_offset % 2
            or call_offset + 2 > len(code) or code[call_offset] != opcode.opmap['CALL']):
        raise CodeBindingError('invalid_callsite_offset')
    cache_count = (opcode._inline_cache_entries[opcode.opmap['CALL']] if sys.version_info[:2] == (3, 12)
                   else opcode._inline_cache_entries['CALL'])
    after_cache = call_offset + 2 * (cache_count + 1)
    if after_cache > len(code) or any(code[index] != opcode.opmap['CACHE']
                                     for index in range(call_offset + 2, after_cache, 2)):
        raise CodeBindingError('unsupported_callsite_shape')
    cached = sys.version_info[:2] == (3, 12)
    observation = after_cache - 2 if cached else call_offset
    profile = 'cpython-312-call-cache-v1' if cached else 'cpython-314-call-v1'
    return CallSite(weakref.ref(caller), weakref.ref(callee), call_offset, observation,
                    caller_probe.digest, callee_probe.digest, profile)
