"""Private passive code fingerprints and weak bindings, not a public witness."""
import hashlib
import importlib.util
import struct
import sys
import threading
from types import CodeType
import weakref

from .witness_state import Probe


MAX_NODES = 4096
MAX_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 64


class CodeBindingError(ValueError):
    """Refuse unsupported compiler metadata without rendering application values."""


class Fingerprint:
    """Encode exact builtin constants only; never use code equality or repr."""

    def __init__(self):
        self.nodes = 0
        self.bytes = 0

    def field(self, kind: bytes, data: bytes) -> bytes:
        self.bytes += len(data) + 9
        if self.bytes > MAX_BYTES:
            raise CodeBindingError('code_byte_capacity')
        return kind + len(data).to_bytes(8) + data

    def integer(self, value: int) -> bytes:
        if value.bit_length() > 4096:
            raise CodeBindingError('integer_capacity')
        return self.field(b'i', bytes([value < 0]) + abs(value).to_bytes(max(1, (value.bit_length() + 7) // 8)))

    def text(self, value: str) -> bytes:
        if len(value) > MAX_BYTES:
            raise CodeBindingError('code_byte_capacity')
        return self.field(b's', value.encode('utf-8', errors='surrogatepass'))

    def sequence(self, value: tuple | frozenset, depth: int, ordered: bool) -> bytes:
        if len(value) > MAX_NODES:
            raise CodeBindingError('code_node_capacity')
        children = [self.value(item, depth + 1) for item in value]
        return self.field(b't' if ordered else b'r',
                          b''.join(children if ordered else sorted(children)))

    def value(self, value: object, depth: int = 0) -> bytes:
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            raise CodeBindingError('code_node_capacity')
        kind = type(value)
        if value is None:
            return self.field(b'n', b'')
        if value is Ellipsis:
            return self.field(b'e', b'')
        if kind is bool:
            return self.field(b'b', bytes([value]))
        if kind is int:
            return self.integer(value)
        if kind is float:
            return self.field(b'f', struct.pack('!d', value))
        if kind is complex:
            return self.field(b'j', struct.pack('!dd', value.real, value.imag))
        if kind is str:
            return self.text(value)
        if kind is bytes:
            return self.field(b'y', value)
        if kind is tuple:
            return self.sequence(value, depth, ordered=True)
        if kind is frozenset:
            return self.sequence(value, depth, ordered=False)
        if kind is CodeType:
            fields = (value.co_argcount, value.co_posonlyargcount, value.co_kwonlyargcount,
                      value.co_nlocals, value.co_stacksize, value.co_flags, value.co_code,
                      value.co_consts, value.co_names, value.co_varnames, value.co_freevars,
                      value.co_cellvars, value.co_filename, value.co_name, value.co_qualname,
                      value.co_firstlineno, value.co_linetable, value.co_exceptiontable)
            return self.field(b'c', self.value(fields, depth + 1))
        raise CodeBindingError('unsupported_code_constant')


def fingerprint(code: CodeType) -> str:
    """Bind original metadata to this concrete CPython compiler/bytecode profile."""
    if (type(code) is not CodeType or sys.implementation.name != 'cpython'
            or sys.version_info[:2] not in ((3, 12), (3, 14))):
        raise CodeBindingError('unsupported_compiler_profile')
    profile = ('otelc-python-code-v1', sys.version_info[:3], sys.implementation.cache_tag,
               importlib.util.MAGIC_NUMBER)
    return hashlib.sha256(Fingerprint().value((profile, code))).hexdigest()


class CodeRegistry:
    """Issue private handles for exact live compiler objects without retaining them."""

    def __init__(self, capacity: int = MAX_NODES):
        if type(capacity) is not int or not 1 <= capacity <= MAX_NODES:
            raise CodeBindingError('code_registry_capacity')
        self.capacity = capacity
        self.entries = {}
        self.lock = threading.RLock()
        self.closed = False

    def register(self, code: CodeType) -> Probe:
        digest = fingerprint(code)
        key = id(code)
        with self.lock:
            if self.closed:
                raise CodeBindingError('code_registry_closed')
            previous = self.entries.get(key)
            if previous is not None and previous[0]() is code:
                return previous[1]
            if len(self.entries) >= self.capacity:
                raise CodeBindingError('code_registry_capacity')
            probe = Probe(digest)

            def expired(reference):
                with self.lock:
                    current = self.entries.get(key)
                    if current is not None and current[0] is reference:
                        self.entries.pop(key)

            self.entries[key] = (weakref.ref(code, expired), probe)
            return probe

    def lookup(self, code: CodeType) -> Probe | None:
        if type(code) is not CodeType:
            return None
        with self.lock:
            entry = self.entries.get(id(code))
            return entry[1] if entry is not None and entry[0]() is code else None

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.entries.clear()
