"""Decode native entry and ordinary-loader reads; reads do not prove execution."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

HEADER = struct.Struct('!8sBBHQQQ')
SOURCE = struct.Struct('!QQQQqQQ')
FINAL = struct.Struct('!7Q')
MAGIC = b'OTLCPY1\0'
MAX_SOURCE = 16 * 1024 * 1024
MAX_JOURNAL = 64 * 1024 * 1024
MAX_RECORDS = 4096
MAX_PATH = 65535


@dataclass(frozen=True)
class SourceInput:
    """Exact bytes supplied to an entry or loader, with opened-file metadata."""

    path: bytes
    content: bytes
    device: int
    inode: int
    mtime_ns: int
    mode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class InputJournal:
    """A finalized capture is distinct from a successful application exit."""

    pid: int
    sources: tuple[SourceInput, ...]
    capture_failures: int
    journal_failures: int
    python_finalized: bool
    entry_expected: bool
    imports: tuple[SourceInput, ...] = ()

    @property
    def complete(self) -> bool:
        """Describe this entry receipt, not imports, SDK health or app success."""
        return (self.python_finalized and self.entry_expected and bool(self.sources)
                and not self.capture_failures and not self.journal_failures)


def decode(data: bytes, *, expected_pid: int) -> InputJournal:
    """Require complete ordered framing and actual launch identity, including EOF."""
    if (type(data) is not bytes or type(expected_pid) is not int
            or expected_pid <= 0 or len(data) > MAX_JOURNAL):
        raise ValueError('invalid native Python journal identity or size')
    offset = sequence = 0
    sources: list[SourceInput] = []
    imports: list[SourceInput] = []
    while offset < len(data):
        if len(data) - offset < HEADER.size:
            raise ValueError('incomplete native Python journal header')
        magic, kind, flags, reserved, current, path_size, size = HEADER.unpack_from(data, offset)
        offset += HEADER.size
        if (magic != MAGIC or kind not in (1, 2, 3) or flags or reserved
                or current != sequence + 1 or current > MAX_RECORDS
                or path_size > MAX_PATH or size > MAX_SOURCE + SOURCE.size
                or size < SOURCE.size or offset + path_size + size > len(data)):
            raise ValueError('invalid or incomplete native Python journal record')
        sequence = current
        path = data[offset:offset + path_size]
        offset += path_size
        payload = data[offset:offset + size]
        offset += size
        if kind == 2:
            return _final(payload, path, offset == len(data), sources, imports, expected_pid)
        pid, device, inode, length, seconds, nanos, mode = SOURCE.unpack_from(payload)
        if (pid != expected_pid or not path or b'\0' in path or nanos >= 10 ** 9
                or not stat.S_ISREG(mode) or length != len(payload) - SOURCE.size):
            raise ValueError('native Python source metadata or identity differs')
        collection = sources if kind == 1 else imports
        collection.append(SourceInput(path, payload[SOURCE.size:], device, inode,
                                      seconds * 10 ** 9 + nanos, mode))
    raise ValueError('native Python journal lacks finalization evidence')


def _final(payload: bytes, path: bytes, at_eof: bool, sources: list[SourceInput],
           imports: list[SourceInput], expected_pid: int) -> InputJournal:
    if path or len(payload) != FINAL.size or not at_eof:
        raise ValueError('invalid native Python journal final record')
    pid, count, size, capture_failures, journal_failures, finalized, expected = FINAL.unpack(payload)
    if (pid != expected_pid or count != len(sources) + len(imports)
            or size != sum(len(source.content) for source in (*sources, *imports))
            or finalized not in (0, 1) or expected not in (0, 1)):
        raise ValueError('native Python journal final counts or identity differ')
    return InputJournal(pid, tuple(sources), capture_failures, journal_failures,
                        bool(finalized), bool(expected), tuple(imports))


def read(path: Path, *, expected_pid: int) -> InputJournal:
    """Read a private journal; the caller must first wait for its owning process."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > MAX_JOURNAL):
            raise ValueError('expected a bounded private native Python journal')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(MAX_JOURNAL + 1)
        after = os.fstat(descriptor)
        if (before.st_size != len(data) or
                (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('native Python journal changed while reading')
    finally:
        os.close(descriptor)
    return decode(data, expected_pid=expected_pid)
