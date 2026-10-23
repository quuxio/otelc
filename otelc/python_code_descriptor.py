"""Describe immutable code graphs without marshal, value formatters or execution."""

from __future__ import annotations

from _json import encode_basestring_ascii, make_encoder
from struct import pack
from sys import getfilesystemencoding, getfilesystemencodeerrors
from types import CodeType


def encode(code: CodeType, *, _factory=make_encoder, _quote=encode_basestring_ascii,
           _pack=pack, _type=type, _hash=object.__hash__, _getattr=getattr,
           _code_type=CodeType, _len=len, _bool=bool, _int=int, _float=float,
           _complex=complex, _str=str, _bytes=bytes, _tuple=tuple,
           _frozenset=frozenset, _slice=slice, _ellipsis=Ellipsis,
           _fs_encoding=getfilesystemencoding, _fs_errors=getfilesystemencodeerrors,
           _value_error=ValueError, _type_error=TypeError) -> bytes:
    """Retain graph aliases with snapshot-local IDs, never application addresses.

    The native caller first checks immutable types and an allocation budget.
    Identity buckets use the raw object hash plus `is`, avoiding both application
    equality/hash overrides and the extra auditing event from Python's id().
    Each invocation owns its graph and C JSON encoder, including marker state.
    """
    nodes: list[dict] = []
    seen: dict[int, list[tuple[object, int]]] = {}

    def visit(value: object, depth: int = 0) -> int:
        if depth > 128:
            raise _value_error('code descriptor depth limit')
        key = _hash(value)
        bucket = seen.get(key, ())
        for previous, identity in bucket:
            if previous is value:
                return identity
        if _len(nodes) >= 65536:
            raise _value_error('code descriptor node limit')
        identity = _len(nodes) + 1
        node: dict = {'id': identity}
        nodes.append(node)
        seen.setdefault(key, []).append((value, identity))
        kind = _type(value)
        if value is None or kind is _bool:
            node.update(type='literal', value=value)
        elif value is _ellipsis:
            node.update(type='ellipsis')
        elif kind is _code_type:
            node.update(type='code', metadata={name: _getattr(value, name) for name in (
                'co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount', 'co_nlocals',
                'co_stacksize', 'co_flags', 'co_firstlineno')}, fields={
                name: visit(_getattr(value, name), depth + 1) for name in (
                    'co_consts', 'co_names', 'co_varnames', 'co_cellvars', 'co_freevars',
                    'co_filename', 'co_name', 'co_qualname', 'co_linetable',
                    'co_exceptiontable', 'co_code')})
        elif kind is _int:
            length = value.bit_length() // 8 + 1
            node.update(type='integer', data=value.to_bytes(length, 'big', signed=True).hex())
        elif kind is _float:
            node.update(type='float', data=_pack('!d', value).hex())
        elif kind is _complex:
            node.update(type='complex', data=_pack('!dd', value.real, value.imag).hex())
        elif kind is _str:
            node.update(type='unicode', value=value)
        elif kind is _bytes:
            node.update(type='bytes', data=value.hex())
        elif kind is _tuple or kind is _frozenset:
            node.update(type='tuple' if kind is _tuple else 'frozenset',
                        items=[visit(item, depth + 1) for item in value])
        elif kind is _slice:
            node.update(type='slice', items=[visit(item, depth + 1)
                                           for item in (value.start, value.stop, value.step)])
        else:
            raise _value_error('unsupported code descriptor value')
        return identity

    def no_default(value: object) -> None:
        raise _type_error('code descriptor contains a non-JSON value')

    # Keep the official immutable C encoder callable, not mutable module-level
    # json.dumps/JSONEncoder globals. It sees only the owned primitive graph.
    try:
        root = visit(code)
        encoder = _factory({}, no_default, _quote, None, ':', ',', True, False, False)
        return ''.join(encoder({'schema_version': 2,
            'filesystem': {'encoding': _fs_encoding(), 'errors': _fs_errors()},
            'root': root, 'nodes': nodes}, 0)).encode('ascii')
    finally:
        # Recursive visit owns a closure cycle. Release application references
        # immediately on success and failure instead of waiting for cyclic GC.
        seen.clear()
        nodes.clear()
        visit = None
