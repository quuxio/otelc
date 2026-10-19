"""Private OTLP socket deadlines; never patch application sockets or DNS."""
import contextvars
import os
import queue
import socket
import sys
import threading
import time
from _thread import LockType
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.util.connection import allowed_gai_family

CURRENT = contextvars.ContextVar("otelc_http_deadline")


@dataclass(eq=False)
class Deadline:
    """Retain only transport state, never the batch, headers or application objects."""
    expires: float
    lock: LockType = field(default_factory=threading.Lock)
    sock: socket.socket | None = None
    finished: bool = False
    expired: bool = False

    def remaining(self) -> float:
        """Reject completion as well as new work after the monotonic deadline."""
        remaining = self.expires - time.monotonic()
        if self.expired or remaining <= 0:
            raise requests.Timeout("OTLP total deadline elapsed")
        return remaining

    def attach(self, sock: socket.socket) -> None:
        """Keep a private TCP descriptor across TLS wrapping and proxy handshakes."""
        with self.lock:
            self.remaining()
            duplicate = socket.socket(fileno=os.dup(sock.fileno()))
            if self.sock is not None:
                self.sock.close()
            self.sock = duplicate

    def cancel(self) -> None:
        """Interrupt idle or trickling reads, including buffered response headers."""
        with self.lock:
            if self.finished:
                return
            self.expired = True
            if self.sock is not None:
                try:
                    # Shutdown the shared TCP connection without mutating TLS state.
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # A completed or failed connection may already be closed.

    def finish(self) -> None:
        """Prevent a late watchdog from interrupting a reused pooled connection."""
        with self.lock:
            self.finished = True
            if self.sock is not None:
                self.sock.close()
            self.sock = None


class Supervisor:
    """One fixed daemon watches at most 64 concurrent export requests."""
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.active: set[Deadline] = set()
        threading.Thread(target=self.watch, name="otelc-http-deadlines", daemon=True).start()

    def add(self, state: Deadline) -> None:
        with self.condition:
            if len(self.active) >= 64:
                raise requests.RequestException("OTLP deadline capacity reached")
            self.active.add(state)
            self.condition.notify()

    def remove(self, state: Deadline) -> None:
        with self.condition:
            self.active.discard(state)
            self.condition.notify()

    def watch(self) -> None:
        """Wake on the earliest expiry without retaining request payloads."""
        while True:
            with self.condition:
                while not self.active:
                    self.condition.wait()
                state = min(self.active, key=lambda value: value.expires)
                remaining = state.expires - time.monotonic()
                if remaining > 0:
                    self.condition.wait(remaining)
                    continue
                self.active.remove(state)
            state.cancel()


@dataclass
class Resolution:
    host: str
    port: int
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    addresses: list = field(default_factory=list)
    error: bool = False


class Resolver:
    """Bound uncancellable OS DNS calls to two daemons and two queued jobs."""
    def __init__(self) -> None:
        self.jobs = queue.Queue(maxsize=2)
        for index in range(2):
            threading.Thread(target=self.resolve, name=f"otelc-http-dns-{index}", daemon=True).start()

    def resolve(self) -> None:
        """Late DNS results cannot initiate a connection or retain a batch."""
        while True:
            job = self.jobs.get()
            try:
                if time.monotonic() < job.deadline:
                    job.addresses = socket.getaddrinfo(job.host, job.port, allowed_gai_family(), socket.SOCK_STREAM)[:64]
            except (OSError, UnicodeError):
                job.error = True
            finally:
                job.done.set()
                self.jobs.task_done()

    def addresses(self, host: str, port: int, state: Deadline) -> list:
        """Return addresses only within the caller's total request budget."""
        job = Resolution(host, port, state.expires)
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            raise requests.RequestException("OTLP DNS capacity reached") from None
        if not job.done.wait(state.remaining()):
            raise requests.Timeout("OTLP DNS deadline elapsed")
        state.remaining()
        if job.error or not job.addresses:
            raise requests.RequestException("OTLP DNS resolution failed")
        return job.addresses


_initialise = threading.Lock()
_supervisor = None
_resolver = None


def workers() -> tuple[Supervisor, Resolver]:
    """Start the fixed transport daemons during runtime setup, before application code."""
    global _supervisor, _resolver
    with _initialise:
        if _supervisor is None:
            _supervisor, _resolver = Supervisor(), Resolver()
    return _supervisor, _resolver


class Connection:
    """Apply request cancellation to urllib3's own TCP and TLS connections."""
    def _new_conn(self) -> socket.socket:
        state = CURRENT.get()
        _, resolver = workers()
        last_error = None
        for family, kind, protocol, _, address in resolver.addresses(self._dns_host, self.port, state):
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(state.remaining())
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.source_address:
                    sock.bind(self.source_address)
                state.attach(sock)
                sock.connect(address)
                sock.settimeout(state.remaining())
                sys.audit("http.client.connect", self, self.host, self.port)
                return sock
            except OSError as error:
                sock.close()
                last_error = error
        raise NewConnectionError(self, "OTLP connection failed") from last_error

    def connect(self) -> None:
        super().connect()
        try:
            # urllib3 SSLTransport represents HTTPS-through-HTTPS proxies.
            sock = self.sock
            while not isinstance(sock, socket.socket):
                sock = sock.socket
            CURRENT.get().attach(sock)
        except Exception:
            self.close()
            raise

    def send(self, data: bytes) -> None:
        state = CURRENT.get()
        state.remaining()
        if self.sock is not None:
            sock = self.sock
            while not isinstance(sock, socket.socket):
                sock = sock.socket
            state.attach(sock)
        super().send(data)


class DeadlineHTTPConnection(Connection, HTTPConnection):
    pass


class DeadlineHTTPSConnection(Connection, HTTPSConnection):
    pass


class HTTPPool(HTTPConnectionPool):
    ConnectionCls = DeadlineHTTPConnection


class HTTPSPool(HTTPSConnectionPool):
    ConnectionCls = DeadlineHTTPSConnection


POOLS = {"http": HTTPPool, "https": HTTPSPool}


class Adapter(HTTPAdapter):
    """Use private pools; application requests and global urllib3 classes stay intact."""
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = POOLS

    def proxy_manager_for(self, *args, **kwargs):
        if args[0].lower().startswith("socks"):
            raise requests.RequestException("unsupported OTLP proxy")
        manager = super().proxy_manager_for(*args, **kwargs)
        manager.pool_classes_by_scheme = POOLS
        return manager
