"""Control framing, endpoint checks and the bind/chmod startup race."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from otelc import control


class ControlTests(unittest.TestCase):
    def test_ready_waits_until_socket_permissions_are_private(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "control.sock"
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(path))
                server.listen(1)
                path.chmod(0o666)

                def serve() -> None:
                    time.sleep(.04)
                    path.chmod(0o600)
                    with server.accept()[0] as client:
                        client.recv(128)
                        client.sendall(json.dumps({"schema_version": 1, "pid": os.getpid(), "collection": False, "emission": False}).encode() + b"\n")

                worker = threading.Thread(target=serve, daemon=True)
                worker.start()
                value = control.wait_ready(path, os.getpid(), timeout=1)
                worker.join(1)
                self.assertEqual(value["pid"], os.getpid())
                self.assertFalse(value["collection"])

    def test_rejects_incomplete_oversized_and_invalid_responses(self) -> None:
        payloads = [b"", b"{" + b"x" * 5000, b"not json\n", b'{"error":"bad"}\n',
                    b'{"schema_version":1,"pid":1,"collection":1,"emission":true}\n']
        for payload in payloads:
            with self.subTest(payload=payload[:40]), tempfile.TemporaryDirectory(dir="/tmp") as directory:
                path = Path(directory) / "socket"
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(path))
                    server.listen(1)
                    path.chmod(0o600)

                    def serve() -> None:
                        with server.accept()[0] as client:
                            client.recv(128)
                            client.sendall(payload)

                    worker = threading.Thread(target=serve, daemon=True)
                    worker.start()
                    with self.assertRaises(ValueError):
                        control.request(path, "status")
                    worker.join(1)

    def test_rejects_wrong_endpoint_and_invalid_command(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "file"
            path.write_text("not a socket")
            with self.assertRaises(ValueError):
                control.request(path, "status")
            path.unlink()
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(path))
                path.chmod(0o600)
                with self.assertRaises(ValueError):
                    control.request(path, "restart")
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            with self.assertRaises(TimeoutError):
                control.wait_ready(Path(directory) / "missing", 1, timeout=.01)
