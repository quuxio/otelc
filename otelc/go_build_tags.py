"""Canonical tag inputs; the pinned Go compiler owns Unicode tag grammar."""
from __future__ import annotations

import string
from typing import Any

ASCII = frozenset(string.ascii_letters + string.digits + '_.')


def canonical(value: Any) -> tuple[str, ...]:
    """Bound literal names without substituting Python's Unicode character tables."""
    if value is None:
        return ()
    if type(value) not in (tuple, list) or len(value) > 128:
        raise ValueError('Go build tags require a bounded list of literal names')
    for name in value:
        if type(name) is not str:
            raise ValueError('Go build tag must be a string')
        try:
            encoded = name.encode('utf-8')
        except UnicodeEncodeError as error:
            raise ValueError('Go build tag must be valid UTF-8') from error
        if not 0 < len(encoded) <= 256 or any(ord(char) < 128 and char not in ASCII for char in name):
            raise ValueError('Go build tag must be one bounded literal compiler tag')
    return tuple(sorted(set(value)))


def record(value: Any) -> tuple[str, ...]:
    """Records must retain the already canonical, nonempty compiler-checked set."""
    tags = canonical(value)
    if type(value) is not list or not tags or list(tags) != value:
        raise ValueError('Go tagged record requires its canonical nonempty tag set')
    return tags


def arguments(tags: tuple[str, ...]) -> list[str]:
    """A single literal Go flag cannot become a list of arbitrary compiler options."""
    return ['-tags=' + ','.join(tags)] if tags else []
