"""Bounded witness records only; no runtime witness capability is enabled here."""
import json
import re
from typing import Any


MAX_FRAME_BYTES = 16 * 1024
MAX_REQUESTS = 16
MAX_EVENTS = 64
MAX_INTEGER = (1 << 53) - 1
_PROFILE = re.compile(r'[a-z][a-z0-9-]{0,63}')
_HEX = re.compile(r'[0-9a-f]+')
_IDS = frozenset(('request_id', 'runtime_id', 'context_id', 'scope_id', 'root_trace_id'))
_DIGESTS = frozenset(('build_id', 'selection_id', 'wrapper_probe_id', 'target_probe_id', 'rendezvous_probe_id'))
_REASONS = frozenset(('unsupported_profile', 'malformed_record', 'unknown_field',
                     'duplicate_field', 'invalid_identity', 'invalid_sequence',
                     'duplicate_request', 'request_capacity', 'event_capacity',
                     'wrong_runtime', 'wrong_context', 'stale_readiness',
                     'wrong_binding', 'invalid_phase', 'scope_reused',
                     'late_activity', 'incomplete', 'export_failed', 'cancelled'))
_OUTCOMES = frozenset(('return', 'exception', 'cancelled', 'incomplete'))
_HEALTH = frozenset(('inflight', 'pending', 'losses', 'export_finished'))
_BINDING = frozenset(('runtime_id', 'context_id', 'scope_id', 'ready_seq',
                      'wrapper_probe_id', 'target_probe_id', 'build_id', 'selection_id'))
_REQUEST = frozenset(('version', 'kind', 'op', 'request_id'))
_RESPONSE = _REQUEST | {'ok', 'runtime_id'}
_EVENT = frozenset(('version', 'kind', 'event', 'event_seq', 'runtime_id'))
_ROOT = _EVENT | {'scope_id', 'root_trace_id', 'root_span_id'}
_REQUEST_FIELDS = {
    'capabilities': _REQUEST | {'profile'},
    'arm': _REQUEST | _BINDING,
    'cancel': _REQUEST | {'runtime_id', 'scope_id'},
}
_EVENT_FIELDS = {
    'ready': _EVENT | {'context_id', 'ready_seq', 'rendezvous_probe_id', 'build_id', 'selection_id'},
    'consumed': _ROOT | {'context_id', 'ready_seq', 'wrapper_probe_id', 'target_probe_id'},
    'root_exit': _ROOT | {'outcome'},
    'end': _ROOT | {'outcome', 'provisional', 'health'},
    'invalidated': _EVENT | {'scope_id', 'reason'},
    'runtime_end': _EVENT | {'scope_id', 'complete', 'health'},
}


class ProtocolError(ValueError):
    """A bounded diagnostic which never contains untrusted record contents."""

    def __init__(self, code: str) -> None:
        code = code if type(code) is str and code in _REASONS else 'malformed_record'
        self.code = code
        super().__init__(code)


def _integer(value: object, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ProtocolError('invalid_sequence')


def _identity(value: object, width: int, nonzero: bool = True) -> None:
    if (type(value) is not str or len(value) != width
            or _HEX.fullmatch(value) is None or nonzero and not int(value, 16)):
        raise ProtocolError('invalid_identity')


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ProtocolError('duplicate_field')
        result[name] = value
    return result


def _number(_: str) -> None:
    raise ProtocolError('invalid_sequence')


def _fields(record: dict[str, Any]) -> set[str] | frozenset[str]:
    kind, op = record.get('kind'), record.get('op')
    if type(kind) is not str:
        raise ProtocolError('malformed_record')
    if kind == 'event':
        event = record.get('event')
        if type(event) is not str or event not in _EVENT_FIELDS:
            raise ProtocolError('malformed_record')
        return _EVENT_FIELDS[event]
    if type(op) is not str or op not in _REQUEST_FIELDS:
        raise ProtocolError('malformed_record')
    if kind == 'request':
        return _REQUEST_FIELDS[op]
    if kind != 'response' or type(record.get('ok')) is not bool:
        raise ProtocolError('malformed_record')
    if not record['ok']:
        return _RESPONSE | {'error'}
    if op == 'arm':
        return _RESPONSE | _BINDING
    if op == 'cancel':
        return _RESPONSE | {'scope_id', 'consumed'}
    if type(record.get('available')) is not bool:
        raise ProtocolError('malformed_record')
    return _RESPONSE | {'profile', 'available'} | (
        {'build_id', 'selection_id'} if record['available'] else {'reason'})


def _identity_field(record: dict[str, Any], name: str, value: Any) -> None:
    nullable = (name == 'scope_id' and record.get('kind') == 'event'
                and record.get('event') in ('invalidated', 'runtime_end'))
    if not nullable or value is not None:
        _identity(value, 32)


def _boolean(name: str, value: Any) -> None:
    if type(value) is not bool or name == 'provisional' and not value:
        raise ProtocolError('malformed_record')


def _choice(record: dict[str, Any], name: str, value: Any) -> None:
    allowed = (_OUTCOMES if name == 'outcome' else
               frozenset(('unsupported_profile', 'disabled', 'invalid_toolchain'))
               if name == 'reason' and record['kind'] == 'response' else _REASONS)
    if type(value) is not str or value not in allowed:
        raise ProtocolError('malformed_record')


def _health(value: Any) -> None:
    if type(value) is not dict or value.keys() != _HEALTH:
        raise ProtocolError('malformed_record')
    for counter in ('inflight', 'pending', 'losses'):
        _integer(value[counter])
    if type(value['export_finished']) is not bool:
        raise ProtocolError('malformed_record')


def _field(record: dict[str, Any], name: str, value: Any) -> None:
    if name in _IDS:
        _identity_field(record, name, value)
    elif name in _DIGESTS:
        _identity(value, 64, nonzero=False)
    elif name == 'root_span_id':
        _identity(value, 16)
    elif name in ('ready_seq', 'event_seq'):
        _integer(value, 1)
    elif name == 'version':
        if type(value) is not int or value != 1:
            raise ProtocolError('malformed_record')
    elif name in ('ok', 'available', 'consumed', 'complete', 'provisional'):
        _boolean(name, value)
    elif name == 'profile':
        if type(value) is not str or _PROFILE.fullmatch(value) is None:
            raise ProtocolError('malformed_record')
    elif name in ('reason', 'error', 'outcome'):
        _choice(record, name, value)
    elif name == 'health':
        _health(value)


def validate_record(record: dict[str, Any]) -> None:
    """Validate structure, never infer freshness, context or state-machine success."""
    if type(record) is not dict:
        raise ProtocolError('malformed_record')
    expected = _fields(record)
    if record.keys() - expected:
        raise ProtocolError('unknown_field')
    if record.keys() != expected:
        raise ProtocolError('malformed_record')
    for name, value in record.items():
        _field(record, name, value)
    if record.get('event') == 'runtime_end' and record['complete']:
        health = record['health']
        if (record['scope_id'] is None or any(health[key] for key in ('inflight', 'pending', 'losses'))
                or not health['export_finished']):
            raise ProtocolError('incomplete')


def decode_record(frame: bytes) -> dict[str, Any]:
    """Read one LF-framed UTF-8 record before accepting its typed structure."""
    if (type(frame) is not bytes or len(frame) > MAX_FRAME_BYTES
            or not frame.endswith(b'\n') or b'\n' in frame[:-1] or b'\r' in frame):
        raise ProtocolError('malformed_record')
    try:
        record = json.loads(frame[:-1].decode('utf-8'), object_pairs_hook=_pairs,
                            parse_float=_number, parse_constant=_number)
    except ProtocolError:
        raise
    except (ValueError, RecursionError):
        raise ProtocolError('malformed_record') from None
    validate_record(record)
    return record


def decode_request(frame: bytes) -> dict[str, Any]:
    """A producer request endpoint never accepts a forged response/event as input."""
    record = decode_record(frame)
    if record['kind'] != 'request':
        raise ProtocolError('malformed_record')
    return record


def encode_record(record: dict[str, Any]) -> bytes:
    """Encode adapter-owned records without accepting arbitrary extra data fields."""
    validate_record(record)
    frame = json.dumps(record, separators=(',', ':'), ensure_ascii=True,
                       allow_nan=False, sort_keys=True).encode('ascii') + b'\n'
    if len(frame) > MAX_FRAME_BYTES:
        raise ProtocolError('malformed_record')
    return frame
