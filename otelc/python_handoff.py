"""Bounded atomic pipe records between Python probes and the owned SDK.

The framing follows the Node handoff: each independently framed chunk fits
POSIX's minimum PIPE_BUF. An abandoned record cannot consume the next one.
JSON carries only primitive telemetry records; application objects are not
pickled or sent to the SDK process.
"""

from __future__ import annotations

import json
import math
import os
import select
import signal
import stat
import struct
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

MAGIC = 0x4F544C43
HEADER = struct.Struct('!IIHHH')
PAYLOAD = 512 - HEADER.size
MAX_RECORD = 16 * 1024 * 1024
MAX_ID = 0xFFFFFFFF


@contextmanager
def pipe_signal_scope() -> Iterator[None]:
    """Consume only a new SIGPIPE caused by this thread's failed SDK write.

    Changing the process handler would change application behaviour. Blocking
    this signal temporarily on the writing thread lets EPIPE become a loss,
    while retaining a signal which was already pending and restoring its mask.
    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGPIPE})
    pending = signal.SIGPIPE in signal.sigpending()
    try:
        yield
    except BrokenPipeError:
        if not pending and signal.SIGPIPE in signal.sigpending():
            signal.sigwait({signal.SIGPIPE})
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate fields instead of accepting an ambiguous record."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate SDK record field')
    return result


def reject_constant(value: str) -> None:
    """JSON NaN and infinity are outside the primitive wire contract."""
    raise ValueError('non-finite SDK record number')


def finite_float(value: str) -> float:
    """Reject finite-looking JSON exponents which overflow a Python float."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('non-finite SDK record number')
    return number


class HandoffWriter:
    """Own one pipe descriptor and bound the complete record's write wait."""

    def __init__(self, descriptor: int, *, timeout_ms: float = 50) -> None:
        if not math.isfinite(timeout_ms) or timeout_ms < 0:
            raise ValueError('invalid SDK handoff timeout')
        metadata = os.fstat(descriptor)
        if not stat.S_ISFIFO(metadata.st_mode):
            raise ValueError('SDK handoff requires a pipe')
        if os.fpathconf(descriptor, 'PC_PIPE_BUF') < 512:
            raise ValueError('SDK handoff pipe has insufficient atomic capacity')
        os.set_blocking(descriptor, False)
        os.set_inheritable(descriptor, False)
        self._descriptor = descriptor
        self._timeout = timeout_ms / 1000
        self._next = 1
        self._closed = False
        self._lock = threading.RLock()
        self._attempted = self._accepted = self._lost = 0

    @property
    def descriptor(self) -> int:
        """Expose the owned descriptor for an explicit child-process handoff."""
        return self._descriptor

    def send(self, record: dict[str, Any]) -> bool:
        """Never turn a probe transport error into an application exception."""
        with self._lock:
            self._attempted += 1
            if self._closed or self._next > MAX_ID:
                self._lost += 1
                return False
            try:
                if type(record) is not dict:
                    raise TypeError('SDK handoff record must be an object')
                data = json.dumps(record, ensure_ascii=False,
                                  allow_nan=False, separators=(',', ':')).encode()
                if not data or len(data) > MAX_RECORD:
                    raise ValueError('oversized SDK handoff record')
                identity = self._next
                self._next += 1
                parts = (len(data) + PAYLOAD - 1) // PAYLOAD
                deadline = time.monotonic() + self._timeout
                with pipe_signal_scope():
                    for part in range(parts):
                        payload = data[part * PAYLOAD:(part + 1) * PAYLOAD]
                        chunk = HEADER.pack(MAGIC, identity, part, parts,
                                            len(payload)) + payload
                        self._write(chunk, deadline)
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                self._lost += 1
                return False
            self._accepted += 1
            return True

    def _write(self, chunk: bytes, deadline: float) -> None:
        while True:
            try:
                if os.write(self._descriptor, chunk) != len(chunk):
                    self.close()
                    raise OSError('partial atomic SDK handoff write')
                return
            except BlockingIOError:
                self._wait_writable(deadline)
            except InterruptedError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('SDK handoff deadline elapsed') from None

    def _wait_writable(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('SDK handoff deadline elapsed') from None
        try:
            select.select([], [self._descriptor], [], remaining)
        except InterruptedError:
            # Resume the write loop with the original deadline.
            return

    def snapshot(self) -> dict[str, int | float]:
        """Count full accepted records separately from attempts and losses."""
        with self._lock:
            return {'attempted': self._attempted, 'accepted': self._accepted,
                    'lost': self._lost, 'deadline_ms': self._timeout * 1000}

    def close(self) -> None:
        """Close this owner once; EOF belongs to all actual pipe writers."""
        with self._lock:
            if not self._closed:
                self._closed = True
                os.close(self._descriptor)

    def seal(self, checkpoint: Callable[[dict[str, int | float]], dict[str, Any]]
             ) -> bool:
        """Take final counts and close atomically with respect to probe writers."""
        with self._lock:
            try:
                return self.send(checkpoint(self.snapshot()))
            finally:
                self.close()


class HandoffReader:
    """Decode complete records and retain incomplete handoffs as loss."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._identity = self._part = self._total = 0
        self._record = bytearray()
        self._finished = False
        self.accepted = self.lost = 0

    def push(self, chunk: bytes) -> list[dict[str, Any]]:
        """Accept arbitrary read fragmentation without losing frame boundaries."""
        if self._finished:
            raise ValueError('SDK handoff reader is finished')
        self._buffer.extend(chunk)
        records = []
        while len(self._buffer) >= HEADER.size:
            magic, identity, part, total, length = HEADER.unpack_from(self._buffer)
            if (magic != MAGIC or identity == 0 or total == 0 or part >= total
                    or length == 0 or length > PAYLOAD):
                raise ValueError('invalid SDK handoff chunk')
            size = HEADER.size + length
            if len(self._buffer) < size:
                break
            self._begin(identity, part, total)
            if (part != self._part or total != self._total
                    or len(self._record) + length > MAX_RECORD):
                raise ValueError('invalid SDK handoff record sequence')
            self._record.extend(self._buffer[HEADER.size:size])
            del self._buffer[:size]
            self._part += 1
            if self._part != total:
                continue
            value = json.loads(self._record, object_pairs_hook=unique_fields,
                               parse_constant=reject_constant,
                               parse_float=finite_float)
            if not isinstance(value, dict):
                raise ValueError('SDK handoff record must be an object')
            records.append(value)
            self.accepted += 1
            self._record.clear()
        return records

    def _begin(self, identity: int, part: int, total: int) -> None:
        if identity == self._identity:
            return
        if identity < self._identity:
            raise ValueError('SDK handoff record identity moved backwards')
        if self._record:
            self.lost += 1
        self._record.clear()
        if part != 0:
            raise ValueError('SDK handoff record begins with a missing chunk')
        self._identity, self._part, self._total = identity, 0, total

    def finish(self) -> None:
        """Make actual EOF final and count an unfinished record once."""
        if not self._finished:
            self._finished = True
            if self._buffer or self._record:
                self.lost += 1
