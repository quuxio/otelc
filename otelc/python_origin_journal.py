"""Read versioned origin relations as data; never infer origin from equal code.

This component validates the wire contract. Authentic observation, both-lane
qualification, process completion and whole-program closure are separate facts.
"""

from __future__ import annotations

import stat
import struct
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from otelc import python_input_journal as inputs
from otelc.python_code_comparison import MAX_STEPS, compare
from otelc.python_code_index import Descriptor, MAX_DEPTH, decode as decode_code

MAGIC = b'OTLCPY2\0'
ROOT = struct.Struct('!10Q')
EXECUTION = struct.Struct('!7Q')
JOIN = struct.Struct('!7Q')
EDGE = struct.Struct('!Q')


@dataclass(frozen=True)
class OriginRoot:
    """One source compiler or explicit cache return and its particular earlier read."""

    identity: int
    transaction: int
    loader: int
    input_sequence: int
    route: int
    compiler_flags: int
    optimize: int
    descriptor: Descriptor


@dataclass(frozen=True)
class Execution:
    """First execution of an opaque code identity through actual constant edges."""

    identity: int
    root: int
    offset: int
    edges: tuple[int, ...]
    node: int
    path: bytes


@dataclass(frozen=True)
class OriginJournal:
    """Closed wire evidence, without a claim of producer or source qualification."""

    pid: int
    sources: tuple[tuple[int, inputs.SourceInput], ...]
    imports: tuple[tuple[int, inputs.SourceInput], ...]
    codes: tuple[inputs.CodeInput, ...]
    roots: tuple[OriginRoot, ...]
    executions: tuple[Execution, ...]
    sdk_joins: tuple[tuple[int, int], ...]
    capture_failures: int
    journal_failures: int
    python_finalized: bool
    entry_expected: bool
    schema_version: int = 2

    @property
    def capture_complete(self) -> bool:
        """Final raw capture health; missing selected bindings remain separate."""
        return self.python_finalized and not self.capture_failures and not self.journal_failures


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError('invalid Python origin journal: ' + reason)


def _frames(data: bytes, version: int = 2) -> Iterator[tuple[int, int, bytes, bytes, bool]]:
    offset = sequence = 0
    while offset < len(data):
        _require(len(data) - offset >= inputs.HEADER.size, 'truncated header')
        magic, kind, flags, reserved, current, path_size, size = inputs.HEADER.unpack_from(data, offset)
        offset += inputs.HEADER.size
        metadata = ROOT.size if kind == 5 else inputs.SOURCE.size
        _require(magic == (MAGIC if version == 2 else b'OTLCPY3\0') and 1 <= kind <= 7 and not flags and not reserved
                 and current == sequence + 1 and current <= inputs.MAX_RECORDS
                 and path_size <= inputs.MAX_PATH and metadata <= size <= metadata + inputs.MAX_SOURCE
                 and offset + path_size + size <= len(data), 'framing or version')
        sequence = current
        path = data[offset:offset + path_size]
        offset += path_size
        payload = data[offset:offset + size]
        offset += size
        _require(not path if kind == 2 else bool(path) and b'\0' not in path, 'path')
        yield kind, sequence, path, payload, offset == len(data)


def _source(payload: bytes) -> inputs.SourceInput:
    _, device, inode, length, seconds, nanos, mode = inputs.SOURCE.unpack_from(payload)
    _require(nanos < 10**9 and stat.S_ISREG(mode)
             and length == len(payload) - inputs.SOURCE.size, 'source metadata')
    # The caller supplies the separately framed original path.
    return inputs.SourceInput(b'', payload[inputs.SOURCE.size:], device, inode,
                              seconds * 10**9 + nanos, mode)


def _code(payload: bytes, path: bytes) -> inputs.CodeInput:
    _, identity, offset, version, descriptor_version, length, reserved = inputs.CODE.unpack_from(payload)
    _require(identity > 0 and offset % 2 == 0 and length > 0
             and length == len(payload) - inputs.CODE.size and not reserved, 'SDK code metadata')
    return inputs.CodeInput(path, payload[inputs.CODE.size:], identity, offset, version, descriptor_version)


def _root(payload: bytes, path: bytes, reads: dict[int, tuple[int, inputs.SourceInput]], journal_version: int) -> OriginRoot:
    (_, identity, transaction, loader, input_sequence, route, version,
     descriptor_version, flags, optimize) = ROOT.unpack_from(payload)
    _require(identity > 0 and transaction > 0 and input_sequence in reads
             and (route in (1, 2) and flags <= 0x7fffffff and optimize <= 2
                  or journal_version == 3 and route == 3 and flags == 0 and optimize == 3), 'root metadata')
    kind, source = reads[input_sequence]
    if route == 3:
        _require(kind == 3 and loader > 0 and source.path.endswith(b'.pyc')
                 and path.endswith(b'.py') and len(source.content) >= 16, 'cache read route')
    else:
        _require(source.path == path and (kind == 1 and loader == 0 if route == 1 else
                                         kind == 3 and loader > 0 and path.endswith(b'.py')), 'read route')
    descriptor = decode_code(inputs.CodeInput(path, payload[ROOT.size:], identity, 0,
                                              version, descriptor_version))
    fields = dict(descriptor.nodes[descriptor.root - 1].fields)
    _require(descriptor.nodes[fields['co_name'] - 1].value == '<module>', 'compiler root')
    return OriginRoot(identity, transaction, loader, input_sequence, route, flags, optimize, descriptor)


def _execution(payload: bytes, path: bytes, roots: dict[int, OriginRoot]) -> Execution:
    _, identity, root, offset, version, depth, reserved = EXECUTION.unpack_from(payload)
    _require(identity > 0 and root in roots and not reserved and depth <= MAX_DEPTH
             and len(payload) == EXECUTION.size + depth * EDGE.size, 'execution metadata')
    descriptor = roots[root].descriptor
    _require(version == descriptor.python_version and offset % 2 == 0, 'execution profile')
    edges = tuple(edge[0] for edge in EDGE.iter_unpack(payload[EXECUTION.size:]))
    node = descriptor.root
    for edge in edges:
        constants = descriptor.nodes[dict(descriptor.nodes[node - 1].fields)['co_consts'] - 1]
        _require(edge < len(constants.references), 'constant edge outside tuple')
        node = constants.references[edge]
        _require(descriptor.nodes[node - 1].kind == 'code', 'constant edge is not code')
    fields = dict(descriptor.nodes[node - 1].fields)
    filename = descriptor.nodes[fields['co_filename'] - 1].value
    _require(filename.encode(descriptor.filesystem_encoding, descriptor.filesystem_errors) == path
             and offset < len(descriptor.nodes[fields['co_code'] - 1].value), 'execution path or offset')
    return Execution(identity, root, offset, edges, node, path)


def decode(data: bytes, *, expected_pid: int, version: int = 2) -> OriginJournal:
    """Reject forward/reused tokens, malformed graphs, mixed versions and loss masks.

    A syntactically valid record cannot prove that a monitoring callback really
    observed its relation. Qualification must independently establish that fact.
    """
    _require(type(version) is int and version in (2, 3)
             and type(data) is bytes and type(expected_pid) is int and expected_pid > 0
             and len(data) <= inputs.MAX_JOURNAL, 'identity or size')
    reads: dict[int, tuple[int, inputs.SourceInput]] = {}
    codes: dict[int, inputs.CodeInput] = {}
    descriptors: dict[int, Descriptor] = {}
    roots: dict[int, OriginRoot] = {}
    executions: dict[int, Execution] = {}
    transactions: set[int] = set()
    consumed: set[int] = set()
    members: set[tuple[int, int]] = set()
    joins: dict[int, int] = {}
    joined_executions: set[int] = set()
    count = size = 0
    budget = MAX_STEPS
    profile: tuple[int, str, str] | None = None
    for kind, sequence, path, payload, at_eof in _frames(data, version):
        _require(inputs.FINAL.unpack_from(payload)[0] == expected_pid, 'process identity')
        if kind == 2:
            _, total, total_size, lost, io_lost, finalized, expected = inputs.FINAL.unpack_from(payload)
            _require(len(payload) == inputs.FINAL.size and at_eof and total == count
                     and total_size == size and finalized in (0, 1) and expected in (0, 1), 'final counts or EOF')
            if not lost and not io_lost:
                _require(set(codes) == set(range(1, len(codes) + 1)), 'missing SDK code identities')
            return OriginJournal(expected_pid,
                tuple((key, value) for key, (route, value) in reads.items() if route == 1),
                tuple((key, value) for key, (route, value) in reads.items() if route == 3),
                tuple(codes.values()), tuple(roots.values()), tuple(executions.values()),
                tuple(joins.items()), lost, io_lost, bool(finalized), bool(expected), version)
        count += 1
        size += len(payload) - (ROOT.size if kind == 5 else inputs.SOURCE.size)
        if kind in (1, 3):
            reads[sequence] = kind, replace(_source(payload), path=path)
        elif kind == 4:
            code = _code(payload, path)
            descriptor = decode_code(code)
            _require(code.identity not in codes, 'duplicate SDK code')
            codes[code.identity] = code
            descriptors[code.identity] = descriptor
        elif kind == 5:
            root = _root(payload, path, reads, version)
            descriptor = root.descriptor
            _require(root.identity not in roots and root.transaction not in transactions
                     and root.input_sequence not in consumed, 'reused root, transaction or read token')
            roots[root.identity] = root
            transactions.add(root.transaction)
            consumed.add(root.input_sequence)
        elif kind == 6:
            execution = _execution(payload, path, roots)
            member = execution.root, execution.node
            _require(execution.identity not in executions and member not in members, 'duplicate execution identity')
            executions[execution.identity] = execution
            members.add(member)
        else:
            _, sdk, origin, offset, a, b, c = JOIN.unpack_from(payload)
            _require(len(payload) == JOIN.size and not (a or b or c)
                     and sdk in codes and origin in executions
                     and sdk not in joins and origin not in joined_executions, 'SDK join metadata or reference')
            execution = executions[origin]
            code = codes[sdk]
            _require(offset == execution.offset == code.offset and path == execution.path == code.path,
                     'SDK join path or offset')
            root = roots[execution.root].descriptor
            comparison = compare(replace(root, root=execution.node), descriptors[sdk], budget=budget)
            budget -= comparison.steps
            _require(comparison.status == 'match', 'SDK descriptor differs from executed member')
            joins[sdk] = origin
            joined_executions.add(origin)
        if kind in (4, 5):
            current_profile = (descriptor.python_version, descriptor.filesystem_encoding,
                               descriptor.filesystem_errors)
            _require(profile is None or profile == current_profile, 'mixed interpreter or filesystem profile')
            profile = current_profile
    raise ValueError('invalid Python origin journal: missing finalization')


def read(path: Path, *, expected_pid: int, version: int = 2) -> OriginJournal:
    """Read only the explicitly selected format after the owning process exits."""
    return decode(inputs._read_bytes(path), expected_pid=expected_pid, version=version)
