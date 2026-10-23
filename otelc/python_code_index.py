"""Validate retained descriptors as data, without constructing or executing code."""

from __future__ import annotations

import json
from codecs import lookup_error
from dataclasses import dataclass

from otelc.python_input_journal import CodeInput, MAX_PATH, MAX_SOURCE

MAX_NODES = 65536
MAX_EDGES = 262144
MAX_DEPTH = 128
METADATA = ('co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount', 'co_nlocals',
            'co_stacksize', 'co_flags', 'co_firstlineno')
NAME_FIELDS = ('co_names', 'co_varnames', 'co_cellvars', 'co_freevars')
TEXT_FIELDS = ('co_filename', 'co_name', 'co_qualname')
BYTE_FIELDS = ('co_linetable', 'co_exceptiontable', 'co_code')
CODE_FIELDS = ('co_consts', *NAME_FIELDS, *TEXT_FIELDS, *BYTE_FIELDS)
Payload = bool | str | bytes | tuple[int, ...] | None


@dataclass(frozen=True)
class Node:
    """One immutable typed value; references are snapshot-local positive IDs."""

    kind: str
    value: Payload = None
    fields: tuple[tuple[str, int], ...] = ()
    metadata: tuple[tuple[str, int], ...] = ()

    @property
    def references(self) -> tuple[int, ...]:
        if self.kind == 'code':
            return tuple(identity for _, identity in self.fields)
        return self.value if type(self.value) is tuple else ()


@dataclass(frozen=True)
class Descriptor:
    """A well-formed descriptor does not establish compilation or source origin."""

    identity: int
    sha256: str
    python_version: int
    descriptor_version: int
    path: bytes
    offset: int
    filesystem_encoding: str
    filesystem_errors: str
    root: int
    nodes: tuple[Node, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError('invalid Python code descriptor: ' + message)


def _pairs(values: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in values:
        _require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def _number(value: str) -> int:
    _require(len(value) <= 11, 'JSON integer exceeds metadata bounds')
    return int(value)


def _not_number(value: str) -> None:
    raise ValueError('invalid Python code descriptor: floating JSON number')


def _reference(value: object) -> int:
    _require(type(value) is int and 1 <= value <= MAX_NODES, 'node reference')
    return value


def _binary(value: object, size: int | None = None) -> bytes:
    _require(type(value) is str and len(value) % 2 == 0
             and all(char in '0123456789abcdef' for char in value), 'hex bytes')
    data = bytes.fromhex(value)
    _require(size is None or len(data) == size, 'binary value width')
    return data


def _code(value: dict) -> Node:
    _require(set(value) == {'id', 'type', 'metadata', 'fields'}, 'code fields')
    metadata, fields = value['metadata'], value['fields']
    _require(type(metadata) is dict and set(metadata) == set(METADATA), 'code metadata')
    _require(all(type(v) is int and 0 <= v <= 0x7fffffff for v in metadata.values()),
             'code metadata integer')
    _require(type(fields) is dict and set(fields) == set(CODE_FIELDS), 'code references')
    return Node('code', fields=tuple((name, _reference(fields[name])) for name in CODE_FIELDS),
                metadata=tuple((name, metadata[name]) for name in METADATA))


def _node(value: object, identity: int) -> Node:
    _require(type(value) is dict and type(value.get('id')) is int
             and value['id'] == identity and type(value.get('type')) is str, 'node identity')
    kind = value['type']
    if kind == 'code':
        return _code(value)
    if kind in ('literal', 'unicode'):
        _require(set(value) == {'id', 'type', 'value'}, 'literal fields')
        data = value['value']
        _require((data is None or type(data) is bool) if kind == 'literal' else type(data) is str,
                 'literal type')
        return Node(kind, data)
    if kind == 'ellipsis':
        _require(set(value) == {'id', 'type'}, 'ellipsis fields')
        return Node(kind)
    if kind in ('integer', 'float', 'complex', 'bytes'):
        _require(set(value) == {'id', 'type', 'data'}, 'binary fields')
        data = _binary(value['data'], {'float': 8, 'complex': 16}.get(kind))
        if kind == 'integer':
            _require(bool(data), 'empty integer')
            integer = int.from_bytes(data, 'big', signed=True)
            _require(len(data) == integer.bit_length() // 8 + 1, 'integer encoding')
        return Node(kind, data)
    _require(kind in ('tuple', 'frozenset', 'slice'), 'unknown node type')
    _require(set(value) == {'id', 'type', 'items'} and type(value['items']) is list,
             'container fields')
    items = tuple(_reference(item) for item in value['items'])
    _require(kind != 'slice' or len(items) == 3, 'slice fields')
    _require(kind != 'frozenset' or len(set(items)) == len(items), 'duplicate set reference')
    return Node(kind, items)


def _shape(node: Node, nodes: tuple[Node, ...]) -> None:
    if node.kind != 'code':
        return
    fields = dict(node.fields)
    for name in ('co_consts', *NAME_FIELDS):
        value = nodes[fields[name] - 1]
        _require(value.kind == 'tuple', 'code tuple field')
        if name != 'co_consts':
            _require(all(nodes[reference - 1].kind == 'unicode' for reference in value.references),
                     'code name tuple')
    for names, kind in ((TEXT_FIELDS, 'unicode'), (BYTE_FIELDS, 'bytes')):
        _require(all(nodes[fields[name] - 1].kind == kind for name in names), 'code field type')
    locals_ = nodes[fields['co_varnames'] - 1]
    _require(dict(node.metadata)['co_nlocals'] == len(locals_.references), 'local count')
    bytecode = nodes[fields['co_code'] - 1].value
    _require(len(bytecode) % 2 == 0, 'bytecode width')


def _graph(nodes: tuple[Node, ...], root: int) -> None:
    _require(sum(len(node.references) for node in nodes) <= MAX_EDGES, 'edge limit')
    for node in nodes:
        _require(all(identity <= len(nodes) for identity in node.references), 'missing node')
    for node in nodes:
        _shape(node, nodes)
    active: set[int] = set()
    heights: dict[int, int] = {}

    def height(identity: int, depth: int) -> int:
        _require(identity not in active, 'graph cycle')
        _require(depth <= MAX_DEPTH, 'graph depth')
        if identity in heights:
            return heights[identity]
        active.add(identity)
        result = max((1 + height(child, depth + 1)
                      for child in nodes[identity - 1].references), default=0)
        active.remove(identity)
        heights[identity] = result
        return result

    try:
        _require(height(root, 0) <= MAX_DEPTH, 'graph depth')
        _require(len(heights) == len(nodes), 'unreachable nodes')
    finally:
        # Break the recursive helper's closure cycle immediately, including
        # failure paths, rather than retaining a large graph until cyclic GC.
        height = None


def decode(value: CodeInput) -> Descriptor:
    """Validate bounded version-two data and its recorded path/entry offset.

    Never construct CodeType, unmarshal payloads, import target modules or invoke
    the application encoder. CPython version stays in the enclosing receipt;
    a future compiler comparison must require that exact interpreter profile.
    """
    _require(type(value) is CodeInput, 'code record type')
    _require(type(value.content) is bytes and 0 < len(value.content) <= MAX_SOURCE, 'payload size')
    _require(type(value.path) is bytes and 0 < len(value.path) <= MAX_PATH
             and b'\0' not in value.path, 'record path')
    _require(type(value.identity) is int and 0 < value.identity < 2**64, 'code identity')
    _require(type(value.offset) is int and 0 <= value.offset < MAX_SOURCE
             and value.offset % 2 == 0, 'code offset')
    _require(type(value.descriptor_version) is int and value.descriptor_version == 2,
             'descriptor version')
    _require(type(value.python_version) is int and 0 <= value.python_version <= 0xffffffff
             and value.python_version >> 24 == 3 and (value.python_version >> 16 & 255) >= 12,
             'Python version')
    try:
        data = json.loads(value.content.decode('ascii'), object_pairs_hook=_pairs, parse_int=_number,
                          parse_float=_not_number, parse_constant=_not_number)
    except (UnicodeError, RecursionError) as error:
        raise ValueError('invalid Python code descriptor: JSON encoding or depth') from error
    _require(type(data) is dict and set(data) == {'schema_version', 'filesystem', 'root', 'nodes'}
             and type(data['schema_version']) is int and data['schema_version'] == 2, 'schema')
    filesystem = data['filesystem']
    _require(type(filesystem) is dict and set(filesystem) == {'encoding', 'errors'}, 'filesystem fields')
    _require(all(type(item) is str and item.isascii() and 0 < len(item) <= 128
                 and all(char.isalnum() or char in '-_.' for char in item)
                 for item in filesystem.values()), 'filesystem codec')
    _require(type(data['nodes']) is list and 0 < len(data['nodes']) <= MAX_NODES, 'node count')
    nodes = tuple(_node(node, identity) for identity, node in enumerate(data['nodes'], 1))
    root = _reference(data['root'])
    _require(root <= len(nodes) and nodes[root - 1].kind == 'code', 'code root')
    _graph(nodes, root)
    fields = dict(nodes[root - 1].fields)
    filename = nodes[fields['co_filename'] - 1].value
    try:
        lookup_error(filesystem['errors'])
        _require(filename.encode(filesystem['encoding'], filesystem['errors']) == value.path,
                 'filename bytes differ')
    except (UnicodeError, LookupError) as error:
        raise ValueError('invalid Python code descriptor: filename encoding') from error
    _require(value.offset < len(nodes[fields['co_code'] - 1].value), 'entry offset outside code')
    return Descriptor(value.identity, value.sha256, value.python_version, value.descriptor_version,
                      value.path, value.offset, filesystem['encoding'], filesystem['errors'], root, nodes)
