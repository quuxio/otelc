"""Stable regular-file identities shared by configuration and launch evidence."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any


def checked_file(filename: Path, *, include_content: bool = False,
                 nofollow: bool = False, max_bytes: int | None = None
                 ) -> tuple[dict[str, Any], bytes]:
    """Bind one regular-file read; optionally retain content without reopening the path."""
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
        raise ValueError('file read bound must be a positive integer')
    filename = filename.absolute()
    descriptor = os.open(filename, os.O_RDONLY | os.O_NONBLOCK | (os.O_NOFOLLOW if nofollow else 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f'launch input is not a regular file: {filename}')
        if max_bytes is not None and before.st_size > max_bytes:
            raise ValueError(f'launch input exceeds its byte bound: {filename}')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            if max_bytes is not None:
                content = source.read(max_bytes + 1)
                if len(content) > max_bytes:
                    raise ValueError(f'launch input exceeds its byte bound: {filename}')
                digest = hashlib.sha256(content).hexdigest()
                if not include_content:
                    content = b''
            else:
                content = source.read() if include_content else b''
                digest = hashlib.sha256(content).hexdigest() if include_content else hashlib.file_digest(source, 'sha256').hexdigest()
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if stable(before) != stable(after) or stable(after) != stable(filename.stat()):
        raise ValueError(f'launch input changed while reading: {filename}')
    result = {'path': str(filename), 'resolved_path': str(filename.resolve(strict=True)),
              'sha256': digest, 'size': after.st_size}
    if filename.is_symlink():
        result['symlink'] = os.readlink(filename)
    return result, content


def identity(filename: Path) -> dict[str, Any]:
    """Record stable input identity without retaining large runtime/tooling contents."""
    return checked_file(filename)[0]
