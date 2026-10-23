"""Compare decoded code graphs as data; equality does not prove their origin."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from otelc.python_code_index import Descriptor, MAX_DEPTH, Node

MAX_STEPS = 1_000_000
_CONTAINERS = {'code', 'tuple', 'frozenset', 'slice'}


@dataclass(frozen=True)
class Comparison:
    """A bounded graph result, independent of source attribution or SDK health."""

    status: Literal['match', 'different', 'incomplete']
    steps: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Work:
    left: int
    right: int
    unordered: bool = False
    next: _Work | None = None


class _Limit(Exception):
    """The comparison exhausted its finite work or branch-depth budget."""


class _Matcher:
    def __init__(self, left: Descriptor, right: Descriptor, budget: int):
        self.left, self.right, self.budget = left, right, budget
        self.steps = 0
        self.left_ids: dict[int, int] = {}
        self.right_ids: dict[int, int] = {}
        self.trail: list[tuple[int, int]] = []
        self.groups: dict[int, dict[bytes, tuple[int, ...]]] = {}
        self.left_hashes: dict[int, bytes] = {}
        self.right_hashes: dict[int, bytes] = {}

    def charge(self, amount: int = 1) -> None:
        self.steps += amount
        if self.steps > self.budget:
            raise _Limit

    def fingerprint(self, nodes: tuple[Node, ...], identity: int,
                    memo: dict[int, bytes], depth: int = 0) -> bytes:
        if depth > MAX_DEPTH:
            raise _Limit
        if identity in memo:
            return memo[identity]
        self.charge()
        node = nodes[identity - 1]
        value = None if node.kind in _CONTAINERS else node.value
        if type(value) is bytes:
            value = value.hex()
        scalar = json.dumps([node.kind, value, node.metadata,
                             [name for name, _ in node.fields]],
                            ensure_ascii=True, separators=(',', ':')).encode('ascii')
        children = [self.fingerprint(nodes, child, memo, depth + 1)
                    for child in node.references]
        if node.kind == 'frozenset':
            children.sort()
        result = hashlib.sha256(scalar + b'\0' + b''.join(children)).digest()
        memo[identity] = result
        return result

    def candidates(self, identity: int, left: int) -> tuple[int, ...]:
        if identity not in self.groups:
            groups: dict[bytes, list[int]] = {}
            for child in self.right.nodes[identity - 1].references:
                self.charge()
                groups.setdefault(self.right_hashes[child], []).append(child)
            self.groups[identity] = {key: tuple(values) for key, values in groups.items()}
        return self.groups[identity].get(self.left_hashes[left], ())

    def rollback(self, checkpoint: int) -> None:
        while len(self.trail) > checkpoint:
            left, right = self.trail.pop()
            del self.left_ids[left]
            del self.right_ids[right]

    def solve(self, pending: _Work | None, depth: int = 0) -> bool:
        if depth > MAX_DEPTH:
            raise _Limit
        while pending is not None:
            self.charge()
            left, right, unordered, pending = (
                pending.left, pending.right, pending.unordered, pending.next)
            if unordered:
                choices = self.candidates(right, left)
                self.charge(len(choices))
                if left in self.left_ids:
                    self.charge()
                    if self.left_ids[left] not in choices:
                        return False
                    continue
                available = tuple(candidate for candidate in choices
                                  if candidate not in self.right_ids)
                if len(available) == 1:
                    pending = _Work(left, available[0], next=pending)
                    continue
                for candidate in available:
                    self.charge()
                    if candidate in self.right_ids:
                        continue
                    checkpoint = len(self.trail)
                    if self.solve(_Work(left, candidate, next=pending), depth + 1):
                        return True
                    self.rollback(checkpoint)
                return False
            if left in self.left_ids:
                if self.left_ids[left] != right:
                    return False
                continue
            if right in self.right_ids or self.left_hashes[left] != self.right_hashes[right]:
                return False
            a, b = self.left.nodes[left - 1], self.right.nodes[right - 1]
            if (a.kind != b.kind or a.metadata != b.metadata
                    or tuple(name for name, _ in a.fields) != tuple(name for name, _ in b.fields)):
                return False
            if a.kind not in _CONTAINERS and a.value != b.value:
                return False
            self.left_ids[left] = right
            self.right_ids[right] = left
            self.trail.append((left, right))
            if len(a.references) != len(b.references):
                return False
            for index in range(len(a.references) - 1, -1, -1):
                pending = _Work(a.references[index], right if a.kind == 'frozenset'
                                else b.references[index], a.kind == 'frozenset', pending)
        return True


def compare(left: Descriptor, right: Descriptor, *, budget: int = MAX_STEPS) -> Comparison:
    """Compare graphs validated by python_code_index.decode, preserving aliases.

    Snapshot-local IDs, recorded entry offsets and filesystem encoding are not
    CodeType values. Filename text, Python version, exact code metadata, bytes,
    constants and graph sharing are compared. Frozenset order is immaterial.
    Structural hashes only filter candidates: exact values and a bidirectional
    node mapping establish a match. Backtracking includes remaining outer
    constraints, so aliases outside an unordered constant remain significant.
    No code object is constructed, compiled, unmarshalled or executed here.
    """
    if type(left) is not Descriptor or type(right) is not Descriptor:
        raise TypeError('code comparison requires decoded descriptors')
    if type(budget) is not int or not 1 <= budget <= MAX_STEPS:
        raise ValueError('invalid code comparison budget')
    if left.python_version != right.python_version:
        return Comparison('incomplete', 0, 'python-version')
    if left.descriptor_version != 2 or right.descriptor_version != 2:
        return Comparison('incomplete', 0, 'descriptor-version')
    matcher = _Matcher(left, right, budget)
    try:
        a = matcher.fingerprint(left.nodes, left.root, matcher.left_hashes)
        b = matcher.fingerprint(right.nodes, right.root, matcher.right_hashes)
        matched = a == b and matcher.solve(_Work(left.root, right.root))
    except (_Limit, RecursionError):
        return Comparison('incomplete', matcher.steps, 'comparison-budget')
    return Comparison('match' if matched else 'different', matcher.steps)
