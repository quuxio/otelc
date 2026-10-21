"""Bounded client for language-independent local metric controls."""

from __future__ import annotations

import json
import os
import socket
import stat
import time
from pathlib import Path
from typing import Any

from otelc import health


def request(path: Path, command: str) -> dict[str, Any]:
    """Send one framed command to a private socket and validate the reply."""
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
        raise ValueError("expected a private control socket")
    if command not in ("status", "on", "off", "collection on", "collection off", "emission on", "emission off", "flush", "health"):
        raise ValueError("unsupported control command")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(3)
        client.connect(str(path))
        client.sendall(command.encode("ascii") + b"\n")
        data = bytearray()
        deadline = time.monotonic() + 3
        while not data.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("control response deadline exceeded")
            client.settimeout(remaining)
            piece = client.recv(1024)
            if not piece or len(data) + len(piece) > 4096:
                raise ValueError("incomplete or oversized control response")
            data.extend(piece)
    value = json.loads(data)
    if command == "health":
        return health.validate(value)
    if value.get("schema_version") != 1 or "error" in value or type(value.get("pid")) is not int:
        raise ValueError("runtime rejected control request")
    if type(value.get("collection")) is not bool or type(value.get("emission")) is not bool:
        raise ValueError("invalid control state")
    return value


def wait_ready(path: Path, pid: int, *, timeout: float = 20.) -> dict[str, Any]:
    """Require the control endpoint to identify the launched process."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.lstat().st_mode & 0o077 == 0:
            value = request(path, "status")
            if value["pid"] != pid:
                raise ValueError("control endpoint belongs to a different process")
            return value
        time.sleep(.02)
    raise TimeoutError("instrumentation control did not become ready")
