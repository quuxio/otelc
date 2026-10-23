"""Small owned-file and bounded reply helpers for the Python SDK worker."""

from __future__ import annotations

import json
import os
import select
import stat
import time
from pathlib import Path
from typing import Any

from otelc.python_handoff import finite_float, reject_constant, unique_fields

MAX_REPLY = 65536


def decode(data: bytes) -> dict[str, Any]:
    """Decode a single unambiguous primitive JSON object."""
    value = json.loads(data, object_pairs_hook=unique_fields,
                       parse_constant=reject_constant, parse_float=finite_float)
    if type(value) is not dict:
        raise ValueError('Python SDK metadata must be an object')
    return value


def inherited_plan(descriptor: int) -> dict[str, Any]:
    """Read only the launcher's inherited, owned private regular file."""
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024 * 1024):
            raise ValueError('Python SDK requires an inherited private plan')
        data = os.read(descriptor, 1024 * 1024 + 1)
        if len(data) != info.st_size:
            raise ValueError('Python SDK plan read was incomplete')
        return decode(data)
    finally:
        os.close(descriptor)


def create_file(path: Path) -> int:
    """Create one fixed runtime output exclusively, preserving existing data."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                   0o600)


def write_json(descriptor: int, value: dict[str, Any]) -> None:
    """Complete an owned regular-file write without accepting a short result."""
    data = json.dumps(value, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    write_bytes(descriptor, data)


def write_bytes(descriptor: int, data: bytes) -> None:
    """Finish an already encoded owned-file payload, including short writes."""
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError('Python SDK report write was incomplete')
        offset += written


def publish(path: Path, value: dict[str, Any]) -> None:
    """Publish new metadata only after its complete private write."""
    data = json.dumps(value, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    publish_bytes(path, data)


def publish_bytes(path: Path, data: bytes) -> None:
    """Atomically publish caller-encoded metadata without replacing any file."""
    staged = path.with_name(path.name + '.staging')
    descriptor = create_file(staged)
    expected = os.fstat(descriptor)
    try:
        write_bytes(descriptor, data)
        os.link(staged, path, follow_symlinks=False)
    finally:
        os.close(descriptor)
        try:
            actual = staged.lstat()
            if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
                staged.unlink()
        except FileNotFoundError:
            pass


def reply(descriptor: int, value: dict[str, Any], *, timeout: float = 1.) -> bool:
    """A disconnected application must not interrupt independent SDK drain."""
    data = json.dumps(value, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    if len(data) > MAX_REPLY:
        return False
    deadline = time.monotonic() + timeout
    offset = 0
    try:
        while offset < len(data):
            try:
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    return False
                offset += written
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                select.select([], [descriptor], [], remaining)
    except OSError:
        return False
    return True
