"""Keep bounded Node launch failure evidence in the test log before cleanup."""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

LIMIT = 64 * 1024
REPORTS = ('node-status.json', 'node-inputs.json', 'node-sources.jsonl',
           'node-plain-inputs.json', 'node-plain-sources.jsonl')


def segments(data: bytes) -> dict[str, Any]:
    """Preserve exact channel bytes, with explicit truncation and offsets."""
    chunks = [(0, data)] if len(data) <= LIMIT else [
        (0, data[:LIMIT // 2]), (len(data) - LIMIT // 2, data[-LIMIT // 2:])]
    return {'size': len(data), 'truncated': len(data) > LIMIT,
            'segments': [{'offset': offset, 'base64': base64.b64encode(chunk).decode('ascii')}
                         for offset, chunk in chunks]}


def report(path: Path) -> dict[str, Any]:
    """Read only the fixed report files, never FIFO or dependency contents."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return {'unavailable': 'not_regular'}
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), 'rb') as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return {'unavailable': 'not_regular'}
            data = handle.read(LIMIT + 1)
            if len(data) <= LIMIT:
                captured = segments(data)
            else:
                size = handle.seek(0, 2)
                handle.seek(size - LIMIT // 2)
                tail = handle.read(LIMIT // 2)
                captured = {'size': size, 'truncated': True,
                            'segments': [{'offset': 0, 'base64': base64.b64encode(data[:LIMIT // 2]).decode('ascii')},
                                         {'offset': size - len(tail), 'base64': base64.b64encode(tail).decode('ascii')}]}
            def identity(value: os.stat_result) -> tuple[int, ...]:
                return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if (identity(before) != identity(opened) or identity(opened) != identity(os.fstat(handle.fileno()))
                    or identity(opened) != identity(path.lstat())):
                return {'unavailable': 'changed_during_read'}
        return captured
    except OSError as error:
        return {'unavailable': type(error).__name__}


def run_checked(command: list[str], *, cwd: Path, runtime: Path,
                timeout: float) -> subprocess.CompletedProcess[str]:
    """Keep launch failure evidence while retaining the original success checks."""
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        state = {'timed_out': True, 'timeout_seconds': timeout,
                 'stdout': segments(error.stdout or b''), 'stderr': segments(error.stderr or b'')}
    else:
        if result.returncode == 0:
            # TextIOWrapper matches the previous subprocess text-mode encoding
            # and universal-newline behaviour; failures still keep raw bytes.
            with io.TextIOWrapper(io.BytesIO(result.stdout)) as stdout, io.TextIOWrapper(io.BytesIO(result.stderr)) as stderr:
                output, errors = stdout.read(), stderr.read()
            return subprocess.CompletedProcess(result.args, result.returncode,
                                               output, errors)
        state = {'timed_out': False, 'returncode': result.returncode,
                 'stdout': segments(result.stdout), 'stderr': segments(result.stderr)}
    evidence = {'kind': 'otelc.test.node-launch-failure', 'diagnostic_only': True,
                'command': command, 'launcher': state,
                'reports': {name: report(runtime / name) for name in REPORTS}}
    raise AssertionError('Node launch failed: ' + json.dumps(evidence, sort_keys=True))
