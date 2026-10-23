"""Keep bounded Node launch failure evidence in the test log before cleanup."""

from __future__ import annotations

import base64
import json
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
        with path.open('rb') as handle:
            data = handle.read(LIMIT + 1)
            if len(data) <= LIMIT:
                return segments(data)
            size = handle.seek(0, 2)
            handle.seek(size - LIMIT // 2)
            tail = handle.read(LIMIT // 2)
        return {'size': size, 'truncated': True,
                'segments': [{'offset': 0, 'base64': base64.b64encode(data[:LIMIT // 2]).decode('ascii')},
                             {'offset': size - len(tail), 'base64': base64.b64encode(tail).decode('ascii')}]}
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
            return subprocess.CompletedProcess(result.args, result.returncode,
                                               result.stdout.decode(), result.stderr.decode())
        state = {'timed_out': False, 'returncode': result.returncode,
                 'stdout': segments(result.stdout), 'stderr': segments(result.stderr)}
    evidence = {'kind': 'otelc.test.node-launch-failure', 'diagnostic_only': True,
                'command': command, 'launcher': state,
                'reports': {name: report(runtime / name) for name in REPORTS}}
    raise AssertionError('Node launch failed: ' + json.dumps(evidence, sort_keys=True))
