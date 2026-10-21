"""One bounded private control stream, separate from application and OTLP channels."""
from collections import deque
import os
from pathlib import Path
import select
import socket
import stat
import threading
import time

from .witness_wire import MAX_EVENTS, MAX_FRAME_BYTES, MAX_REQUESTS


class WitnessControl:
    """Own one socket and connection; never keep application shutdown waiting."""

    def __init__(self, path: str, state) -> None:
        self.path = Path(os.path.abspath(path))
        parent = self.path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid() or parent.st_mode & 0o077):
            raise ValueError('witness control directory must be owned by this user with mode 0700')
        self.state = state
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connection = None
        self.identity = None
        self.deadline = None
        self.closed = False
        try:
            self.listener.bind(str(self.path))
            metadata = self.path.lstat()
            self.identity = metadata.st_dev, metadata.st_ino
            self.path.chmod(0o600)
            self.listener.listen(1)
            self.listener.setblocking(False)
            self.worker = threading.Thread(target=self._serve, name='otelc-witness-control', daemon=True)
            self.worker.start()
        except BaseException:
            self.listener.close()
            self._unlink()
            raise

    def _unlink(self) -> None:
        try:
            metadata = self.path.lstat()
            if (self.identity == (metadata.st_dev, metadata.st_ino)
                    and stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid()):
                self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            self.state.invalidate('incomplete')

    def _serve(self) -> None:
        outgoing = deque()
        incoming = bytearray()
        input_closed = False
        refused = False
        try:
            while not self.closed:
                # Producer records remain bounded in WitnessState until space exists.
                if len(outgoing) <= MAX_REQUESTS:
                    outgoing.extend(self.state.drain())
                if self.deadline is not None:
                    if time.monotonic() >= self.deadline:
                        if outgoing:
                            self.state.invalidate('incomplete')
                        break
                    if not outgoing and self.state.terminal_emitted:
                        break
                connection = self.connection
                if connection is None:
                    ready, _, _ = select.select([self.listener], [], [], 0.005)
                    if ready:
                        self.connection, _ = self.listener.accept()
                        self.connection.setblocking(False)
                        self.listener.close()  # This runtime never accepts a replacement consumer.
                    continue
                readable, writable, _ = select.select(
                    [] if input_closed or refused else [connection],
                    [connection] if outgoing else [], [], 0.005)
                if writable:
                    sent = connection.send(outgoing[0])
                    if not sent:
                        raise OSError('closed witness stream')
                    outgoing[0] = outgoing[0][sent:]
                    if not outgoing[0]:
                        outgoing.popleft()
                if readable:
                    data = connection.recv(min(4096, MAX_FRAME_BYTES - len(incoming)))
                    if not data:
                        input_closed = True
                        if incoming:
                            self.state.invalidate('malformed_record')
                            refused = True
                    else:
                        incoming.extend(data)
                        while b'\n' in incoming and not refused:
                            end = incoming.index(b'\n') + 1
                            frame = bytes(incoming[:end])
                            del incoming[:end]
                            if len(outgoing) >= MAX_EVENTS + MAX_REQUESTS:
                                self.state.invalidate('event_capacity')
                                refused = True
                                break
                            response = self.state.handle(frame)
                            if response is None:
                                refused = True
                            else:
                                outgoing.append(response)
                        if len(incoming) == MAX_FRAME_BYTES:
                            self.state.invalidate('malformed_record')
                            refused = True
                # Flush the trusted refusal event, then terminate this session.
                if refused and not outgoing:
                    outgoing.extend(self.state.drain())
                    if not outgoing:
                        break
        except (OSError, ValueError):
            if not self.closed:
                self.state.invalidate('incomplete')
        finally:
            self.listener.close()
            if self.connection is not None:
                self.connection.close()

    def close(self) -> None:
        """Flush final records briefly; an absent or stalled reader cannot hold exit."""
        if self.closed:
            return
        self._unlink()
        self.deadline = time.monotonic() + 0.1
        self.worker.join(0.15)
        if self.worker.is_alive():
            self.state.invalidate('incomplete')
        self.closed = True
        self.listener.close()
        if self.connection is not None:
            self.connection.close()
