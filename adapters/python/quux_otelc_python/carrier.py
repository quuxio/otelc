"""Bounded W3C values; local recording ownership is kept by TraceStore."""
import re
from collections.abc import Sequence
from dataclasses import dataclass

from opentelemetry.trace import SpanContext, TraceFlags, TraceState

_PARENT = re.compile(r'([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})')
_KEY = re.compile(r'[a-z][_0-9a-z*/-]{0,255}|[a-z0-9][_0-9a-z*/-]{0,240}@[a-z][_0-9a-z*/-]{0,13}')
_KNOWN_FLAGS = 3  # Sampled and the SDK random-trace-ID extension.


@dataclass(frozen=True)
class Context:
    """Validated wire identity, independent of local trees and SDK validators."""
    trace_id: int
    span_id: int
    trace_flags: int
    tracestate: str | None

    def __post_init__(self) -> None:
        if (type(self.trace_id) is not int or not 0 < self.trace_id < 1 << 128
                or type(self.span_id) is not int or not 0 < self.span_id < 1 << 64
                or type(self.trace_flags) is not int or not 0 <= self.trace_flags <= _KNOWN_FLAGS
                or self.tracestate is not None and (
                    type(self.tracestate) is not str
                    or _state([self.tracestate]) != self.tracestate)):
            raise ValueError('Context requires valid IDs, known flags and canonical state')


def _pairs(fields: Sequence[str]) -> list[tuple[str, str]] | None:
    if len(fields) > 32 or any(type(value) is not str for value in fields):
        return None
    if sum(len(value) for value in fields) + max(0, len(fields) - 1) > 32768:
        return None
    if any(not value.isascii() for value in fields):
        return None
    members = ','.join(fields).split(',', 32)
    if len(members) > 32:
        return None
    pairs, keys = [], set()
    for member in members:
        member = member.strip(' \t')
        if not member:
            continue
        key, separator, value = member.partition('=')
        if (not separator or _KEY.fullmatch(key) is None or key in keys
                or not 1 <= len(value) <= 256
                or any(not 0x20 <= ord(char) <= 0x7e or char in ',=' for char in value)):
            return None
        keys.add(key)
        pairs.append((key, value))
    return pairs


def _state(fields: Sequence[str]) -> str | None:
    pairs = _pairs(fields)
    if pairs is None:
        return None

    def size() -> int:
        return sum(len(key) + len(value) + 1 for key, value in pairs) + max(0, len(pairs) - 1)

    for index in range(len(pairs) - 1, -1, -1):
        if size() <= 512:
            break
        if len(pairs[index][0]) + len(pairs[index][1]) + 1 > 128:
            pairs.pop(index)
    while size() > 512:
        pairs.pop()
    return ','.join(f'{key}={value}' for key, value in pairs) or None


def extract(parents: Sequence[str], states: Sequence[str] = ()) -> Context | None:
    """Read all field occurrences; bad state never invalidates a valid parent."""
    if len(parents) != 1 or type(parents[0]) is not str or len(parents[0]) > 512:
        return None
    try:
        if len(parents[0].encode('utf-8')) > 512:
            return None
    except UnicodeEncodeError:
        return None
    value = parents[0].strip(' \t')
    if any(ord(char) < 0x20 and char != '\t' or ord(char) == 0x7f for char in value):
        return None
    match = _PARENT.match(value)
    if match is None:
        return None
    version, trace, parent, flags = match.groups()
    if (version == 'ff' or version == '00' and len(value) != 55
            or len(value) > 55 and value[55] != '-' or int(trace, 16) == 0 or int(parent, 16) == 0):
        return None
    return Context(int(trace, 16), int(parent, 16), int(flags, 16) & _KNOWN_FLAGS, _state(states))


def from_sdk(context: SpanContext) -> Context | None:
    """Copy a concrete SDK identity into the validated wire representation."""
    if (type(context) is not SpanContext or not context.is_valid
            or type(context.trace_id) is not int or type(context.span_id) is not int
            or type(context.trace_flags) not in (int, TraceFlags) or not 0 <= context.trace_flags <= 255
            or type(context.trace_state) is not TraceState):
        return None
    return Context(context.trace_id, context.span_id, context.trace_flags & _KNOWN_FLAGS,
                   _state([context.trace_state.to_header()]))


def inject(context: Context) -> dict[str, str]:
    """Emit a version-00 parent, known flags and bounded canonical state."""
    result = {'traceparent': f'00-{context.trace_id:032x}-{context.span_id:016x}-{context.trace_flags:02x}'}
    if context.tracestate:
        result['tracestate'] = context.tracestate
    return result
