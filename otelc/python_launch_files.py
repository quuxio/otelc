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
        stable = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                               info.st_mode, info.st_uid, info.st_gid)
        changed = (stable(before) != stable(after) or before.st_ctime_ns != after.st_ctime_ns
                   or before.st_nlink != after.st_nlink)
        if changed and (stable(before) == stable(after)
                        and before.st_nlink == 2 and after.st_nlink == 1
                        and after.st_ctime_ns >= before.st_ctime_ns):
            # Atomic hard-link publication exposes complete bytes before the
            # staging name is removed. That unlink alone can change ctime.
            # Confirm identical bytes on the same descriptor with stable final
            # metadata; do not accept a concurrent rewrite or a general retry.
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                stream.seek(0)
                confirmed = stream.read(MAX_METADATA + 1)
            final = os.fstat(descriptor)
            changed = (confirmed != data or stable(after) != stable(final)
                       or after.st_ctime_ns != final.st_ctime_ns
                       or after.st_nlink != final.st_nlink)
        if len(data) != before.st_size or changed:
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
