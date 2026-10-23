"""Bounded owned metadata for the fixed Python launch, separate from app output."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from otelc import python_recipe as recipe
from otelc import python_sdk_io as io

MAX_METADATA = 64 * 1024 * 1024


def private_directory(path: Path) -> Path:
    """Require the selected owned private leaf without changing existing data."""
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('Python runtime directory must be owned and mode 0700')
    return path.resolve(strict=True)


def read_bytes(path: Path) -> bytes:
    """Read one fixed private regular file, refusing partial or changing input."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > MAX_METADATA):
            raise ValueError('invalid private Python launch metadata')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            data = source.read(MAX_METADATA + 1)
        after = os.fstat(descriptor)
        stable = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(data) != before.st_size or stable(before) != stable(after):
            raise ValueError('Python launch metadata changed while reading')
        return data
    finally:
        os.close(descriptor)


def read(path: Path) -> tuple[dict[str, Any], bytes]:
    """Decode bounded JSON without accepting duplicate fields or non-finite values."""
    data = read_bytes(path)
    return io.decode(data), data


def write(path: Path, value: dict[str, Any]) -> None:
    """Create one canonical recipe file exclusively; never replace user data."""
    data = recipe.encoded(value).encode()
    if len(data) > MAX_METADATA:
        raise ValueError('Python launch metadata exceeds its bound')
    io.publish_bytes(path, data)
