"""Bounded client for language-independent local metric controls."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import time
from pathlib import Path
from typing import Any

from otelc import health


def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse ambiguous endpoint metadata without logging its authentication key."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate control endpoint field")
    return result


def endpoint_descriptor(path: Path, expected: os.stat_result) -> dict[str, Any]:
    """Read a complete private loopback descriptor through a stable regular-file handle."""
    if path.name != "control.json":
        raise ValueError("expected the generated control.json endpoint")
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError("control endpoint directory must be owned and private (0700)")
        raw = _read_endpoint(directory, expected)
    finally:
        os.close(directory)
    value = json.loads(raw, object_pairs_hook=unique_fields)
    fields = {"schema_version", "kind", "host", "port", "pid", "token"}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != "otelc.control.tcp" or value["host"] != "127.0.0.1"
            or type(value["port"]) is not int or not 1 <= value["port"] <= 65535
            or type(value["pid"]) is not int or value["pid"] <= 0
            or not isinstance(value["token"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["token"])):
        raise ValueError("invalid private control endpoint")
    return value


def _read_endpoint(directory: int, expected: os.stat_result) -> bytes:
    """Anchor a fixed basename; a replacement FIFO must not block before fstat."""
    descriptor = os.open("control.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        actual = os.fstat(descriptor)
        if ((actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
                or not stat.S_ISREG(actual.st_mode) or actual.st_mode & 0o077 or actual.st_uid != os.getuid()):
            raise ValueError("control endpoint identity changed")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError("oversized control endpoint")
        return raw
    finally:
        os.close(descriptor)


def exchange(client: socket.socket, payload: str) -> Any:
    """Keep both transports on the same bounded framing and response deadline."""
    client.settimeout(3)
    client.sendall(payload.encode("ascii") + b"\n")
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
    return json.loads(data)


def validate_response(value: Any, command: str, expected_pid: int | None) -> dict[str, Any]:
    """Check common replies without exposing endpoint authentication material."""
    if not isinstance(value, dict):
        raise ValueError("invalid control response")
    if expected_pid is not None and value.get("pid") != expected_pid and "error" not in value:
        raise ValueError("control endpoint belongs to a different process")
    if command == "health":
        return health.validate(value)
    if value.get("schema_version") != 1 or "error" in value or type(value.get("pid")) is not int:
        raise ValueError("runtime rejected control request")
    if type(value.get("collection")) is not bool or type(value.get("emission")) is not bool:
        raise ValueError("invalid control state")
    return value


def request(path: Path, command: str) -> dict[str, Any]:
    """Send one framed command to a private socket or authenticated loopback endpoint."""
    metadata = path.lstat()
    if (not (stat.S_ISSOCK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
            or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid()):
        raise ValueError("expected a private control socket or endpoint")
    if command not in ("status", "on", "off", "collection on", "collection off", "emission on", "emission off", "flush", "health"):
        raise ValueError("unsupported control command")
    endpoint = endpoint_descriptor(path, metadata) if stat.S_ISREG(metadata.st_mode) else None
    if endpoint:
        # Never use a user-derived hostname, including after descriptor validation.
        with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=3) as client:
            value = exchange(client, json.dumps({"token": endpoint["token"], "command": command}))
    else:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(str(path))
            value = exchange(client, command)
    return validate_response(value, command, endpoint["pid"] if endpoint else None)


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
