"""Bounded live metric controls for the running Python application."""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any

from otelc import python_sdk_io as io
from otelc.python_runtime import PythonRuntime


class ControlServer:
    """Use the existing authenticated loopback control descriptor/protocol."""

    def __init__(self, runtime: PythonRuntime, directory: Path) -> None:
        self.runtime = runtime
        self.token = secrets.token_hex(32)
        self._stop = threading.Event()
        self._listener = socket.socket()
        self._thread = threading.Thread(target=self._serve,
                                        name='otelc-python-control', daemon=True)
        try:
            self._listener.bind(('127.0.0.1', 0))
            self._listener.listen(8)
            self._listener.settimeout(.2)
            self._thread.start()
            io.publish(directory / 'control.json', {
                'schema_version': 1, 'kind': 'otelc.control.tcp',
                'host': '127.0.0.1', 'port': self._listener.getsockname()[1],
                'pid': os.getpid(), 'token': self.token,
            })
        except BaseException:
            self.close()
            raise

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with client:
                client.settimeout(.2)
                try:
                    response = self._handle(client)
                    data = json.dumps(response, separators=(',', ':')).encode() + b'\n'
                    client.sendall(data)
                except (OSError, ValueError, RuntimeError):
                    continue

    def _handle(self, client: socket.socket) -> dict[str, Any]:
        data = bytearray()
        deadline = time.monotonic() + .2
        while not data.endswith(b'\n'):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Python control input deadline elapsed')
            client.settimeout(remaining)
            piece = client.recv(1024)
            if not piece or len(data) + len(piece) > 4096:
                raise ValueError('invalid Python control request')
            data.extend(piece)
        value = io.decode(bytes(data))
        if (set(value) != {'token', 'command'} or type(value['token']) is not str
                or not secrets.compare_digest(value['token'], self.token)
                or type(value['command']) is not str):
            raise ValueError('invalid Python control authentication')
        try:
            return self.command(value['command'])
        except (ValueError, RuntimeError, OSError):
            return {'schema_version': 1, 'pid': os.getpid(), 'error': 'control operation failed'}

    def command(self, command: str) -> dict[str, Any]:
        """Serialise control callers; the application continues during SDK waits."""
        state = self.runtime.status()
        collection, emission = state['collection'], state['emission']
        if command == 'status':
            sdk = self.runtime.channel.request('status', timeout=2.5)
            return {**state, 'effective_emission_off': sdk['effective_emission_off'],
                    'in_flight_exports': sdk['in_flight_exports']}
        if command == 'flush':
            self.runtime.channel.request('flush', timeout=2.5)
            return self.runtime.status()
        switches = {
            'on': (True, True), 'off': (False, False),
            'collection on': (True, emission), 'collection off': (False, emission),
            'emission on': (collection, True), 'emission off': (collection, False),
        }
        if command not in switches:
            raise ValueError('unsupported Python control command')
        return self.runtime.controls(*switches[command])

    def close(self) -> None:
        """An incomplete client cannot hold up native application shutdown."""
        self._stop.set()
        self._listener.close()
        if self._thread.ident is not None and threading.current_thread() is not self._thread:
            self._thread.join(timeout=.5)
