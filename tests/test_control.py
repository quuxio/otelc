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
        payloads = [b"", b"{" + b"x" * 5000, b"not json\n", b"[]\n", b'{"error":"bad"}\n',
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

    def test_private_loopback_descriptor_authentication_and_pid_binding(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "control.json"
            with socket.socket(socket.AF_INET) as server:
                server.bind(("127.0.0.1", 0))
                server.listen(2)
                endpoint = {"schema_version": 1, "kind": "otelc.control.tcp", "host": "127.0.0.1",
                            "port": server.getsockname()[1], "pid": os.getpid(), "token": "0" * 64}
                path.write_text(json.dumps(endpoint))
                path.chmod(0o600)
                seen = []

                def serve() -> None:
                    for pid in (os.getpid(), os.getpid() + 1):
                        with server.accept()[0] as client:
                            data = bytearray()
                            while not data.endswith(b"\n"):
                                data.extend(client.recv(1024))
                            request = json.loads(data)
                            seen.append(request["command"] == "status" and request["token"] == endpoint["token"])
                            client.sendall(json.dumps({"schema_version": 1, "pid": pid,
                                "collection": False, "emission": True}).encode() + b"\n")

                worker = threading.Thread(target=serve, daemon=True)
                worker.start()
                state = control.wait_ready(path, os.getpid(), timeout=1)
                self.assertFalse(state["collection"])
                self.assertTrue(state["emission"])
                with self.assertRaisesRegex(ValueError, "different process"):
                    control.request(path, "status")
                worker.join(1)
                self.assertEqual(seen, [True, True])

    def test_endpoint_metadata_refuses_ambiguous_remote_public_or_replaced_inputs(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "control.json"
            valid = {"schema_version": 1, "kind": "otelc.control.tcp", "host": "127.0.0.1",
                     "port": 12345, "pid": os.getpid(), "token": "0" * 64}
            changes = [{"schema_version": True}, {"kind": "other"}, {"host": "example.com"},
                       {"host": "localhost"}, {"port": 0}, {"port": True}, {"port": 65536},
                       {"pid": False}, {"pid": 0}, {"token": "bad"}, {"extra": 1}]
            for index, change in enumerate(changes):
                path.write_text(json.dumps(valid | change))
                path.chmod(0o600)
                with self.subTest(index=index), self.assertRaises(ValueError):
                    control.request(path, "status")
            for payload in ('[]', '{"schema_version":1,"schema_version":1}', 'x' * 4097):
                path.write_text(payload)
                with self.assertRaises(ValueError):
                    control.request(path, "status")
            path.write_text(json.dumps(valid))
            original = path.stat()
            path.rename(path.with_suffix(".old"))
            path.write_text(json.dumps(valid))
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "identity changed"):
                control.endpoint_descriptor(path, original)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private control"):
                control.request(path, "status")
