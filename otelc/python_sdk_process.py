"""Supervise the isolated Python SDK without owning application execution."""

from __future__ import annotations

import hashlib
import json
import math
import os
import select
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from otelc.config import Policy
from otelc.python_handoff import HandoffWriter, MAX_ID
from otelc import python_sdk_io as io
from otelc import python_sdk_protocol as wire

ROOT = Path(__file__).resolve().parent.parent


def policy_digest(policy: Policy) -> str:
    """Bind every resolved common-policy field in SDK replies and reports."""
    return hashlib.sha256(json.dumps(
        policy.data, sort_keys=True, separators=(',', ':'),
    ).encode()).hexdigest()


def private_json(path: Path) -> dict[str, Any]:
    """Read bounded owned metadata through one fixed, non-following handle."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > io.MAX_REPLY):
            raise ValueError('invalid Python SDK runtime metadata')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            data = source.read(io.MAX_REPLY + 1)
        if len(data) != info.st_size:
            raise ValueError('Python SDK metadata read was incomplete')
        return io.decode(data)
    finally:
        os.close(descriptor)


class SdkChannel:
    """Application-side primitive pipe/RPC channel; no SDK or subprocess work."""

    def __init__(self, events: int, replies: int, *, sdk_pid: int,
                 launch_id: str, policy_sha256: str) -> None:
        if not stat.S_ISFIFO(os.fstat(replies).st_mode):
            raise ValueError('Python SDK reply channel requires a pipe')
        self.writer = HandoffWriter(events)
        os.set_blocking(replies, False)
        os.set_inheritable(replies, False)
        self._replies = replies
        self._reply_closed = False
        self._buffer = bytearray()
        self._rpc_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._next = 1
        self.sdk_pid = sdk_pid
        self.launch_id = launch_id
        self.policy_sha256 = policy_sha256
        self.application_pid: int | None = None

    @property
    def descriptors(self) -> tuple[int, int]:
        """Return only the descriptors an explicitly launched app inherits."""
        return self.writer.descriptor, self._replies

    def bind(self, pid: int, *, timeout: float = 10.) -> dict[str, Any]:
        """Initialise the SDK with this native application's actual identity."""
        deadline = self._deadline(timeout)
        if not self._rpc_lock.acquire(timeout=timeout):
            raise TimeoutError('Python SDK request is still pending')
        try:
            if self.application_pid is not None:
                raise RuntimeError('Python SDK channel is already bound')
            self.application_pid = wire.integer(pid, minimum=1)
            return self._exchange({'kind': 'hello', 'pid': pid,
                                   'launch_id': self.launch_id},
                                  deadline - time.monotonic())
        finally:
            self._rpc_lock.release()

    def request(self, command: str, *, timeout: float = 3.) -> dict[str, Any]:
        """A status reply never stands in for decoded downstream telemetry."""
        if self.application_pid is None or command not in ('status', 'flush'):
            raise ValueError('invalid or unbound Python SDK request')
        return self._exchange({'kind': 'request', 'command': command}, timeout)

    def controls(self, *, collection: bool, emission: bool,
                 timeout: float = 3.) -> dict[str, Any]:
        """Order switch changes in the same stream as primitive observations."""
        if self.application_pid is None:
            raise RuntimeError('Python SDK channel is not bound')
        deadline = self._deadline(timeout)
        if not self._rpc_lock.acquire(timeout=timeout):
            raise TimeoutError('Python SDK request is still pending')
        try:
            record = {'kind': 'controls', 'collection': wire.flag(collection),
                      'emission': wire.flag(emission)}
            if not self.writer.send(record):
                raise RuntimeError('Python SDK control handoff failed')
            status = self.request('status', timeout=deadline - time.monotonic())
            if (status['collection'], status['emission']) != (collection, emission):
                raise ValueError('Python SDK control acknowledgement differs')
            return status
        finally:
            self._rpc_lock.release()

    @staticmethod
    def _deadline(timeout: float) -> float:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('invalid Python SDK reply timeout')
        return time.monotonic() + timeout

    def _exchange(self, record: dict[str, Any], timeout: float) -> dict[str, Any]:
        deadline = self._deadline(timeout)
        if not self._rpc_lock.acquire(timeout=timeout):
            raise TimeoutError('Python SDK request is still pending')
        try:
            if self._next > MAX_ID:
                raise RuntimeError('Python SDK request identity exhausted')
            identity = self._next
            self._next += 1
            if not self.writer.send({**record, 'id': identity}):
                raise RuntimeError('Python SDK request handoff failed')
            result = self._receive(identity, deadline)
            if not result['ok']:
                raise RuntimeError('Python SDK operation failed or remains pending')
            return result['status']
        finally:
            self._rpc_lock.release()

    def _receive(self, identity: int, deadline: float) -> dict[str, Any]:
        while True:
            if b'\n' in self._buffer:
                value = self._buffer_reply()
                received = value['id']
                if received == identity:
                    return value
                if received > identity:
                    raise ValueError('Python SDK reply identity differs')
                # A previous timed-out caller can receive its actual late reply.
                continue
            self._read_more(deadline)

    def _buffer_reply(self) -> dict[str, Any]:
        line, _, rest = self._buffer.partition(b'\n')
        self._buffer[:] = rest
        if len(line) + 1 > io.MAX_REPLY:
            raise ValueError('oversized Python SDK reply')
        value = io.decode(line)
        wire.fields(value, {'id', 'ok', 'status'})
        wire.integer(value['id'], minimum=1, maximum=MAX_ID)
        wire.flag(value['ok'])
        self._identity(value['status'])
        return value

    def _read_more(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Python SDK reply deadline elapsed')
        ready, _, _ = select.select([self._replies], [], [], remaining)
        if not ready:
            raise TimeoutError('Python SDK reply deadline elapsed')
        data = os.read(self._replies, io.MAX_REPLY + 1)
        if not data:
            raise ValueError('incomplete or oversized Python SDK reply')
        self._buffer.extend(data)
        if b'\n' not in self._buffer and len(self._buffer) > io.MAX_REPLY:
            raise ValueError('oversized Python SDK reply')

    def _identity(self, value: Any) -> None:
        if (type(value) is not dict or value.get('schema_version') != 1
                or type(value.get('schema_version')) is not int
                or value.get('kind') != 'otelc.python.sdk'
                or type(value.get('sdk_pid')) is not int
                or type(value.get('pid')) is not int
                or value.get('sdk_pid') != self.sdk_pid
                or value.get('pid') != self.application_pid
                or value.get('launch_id') != self.launch_id
                or value.get('policy_sha256') != self.policy_sha256):
            raise ValueError('Python SDK reply belongs to a different launch')
        wire.flag(value.get('collection'))
        wire.flag(value.get('emission'))

    def seal(self, producer: dict[str, Any]) -> bool:
        """Send producer facts and an atomic handoff checkpoint, then actual EOF."""
        if self.application_pid is None or producer.keys() & {'pid', 'launch_id', 'handoff'}:
            raise ValueError('invalid Python producer checkpoint identity')
        return self.writer.seal(lambda counts: {'kind': 'checkpoint', 'status': {
            **producer, 'pid': self.application_pid,
            'launch_id': self.launch_id, 'handoff': counts,
        }})

    def close(self) -> None:
        """Close owned descriptor copies without inventing a final checkpoint."""
        with self._close_lock:
            self.writer.close()
            if not self._reply_closed:
                self._reply_closed = True
                os.close(self._replies)


class SdkProcess:
    """CLI-owned SDK worker; the application receives only inherited channels."""

    def __init__(self, process: subprocess.Popen[bytes], channel: SdkChannel,
                 directory: Path, policy: Policy) -> None:
        self.process = process
        self.channel = channel
        self.directory = directory
        self.policy = policy

    @classmethod
    def start(cls, policy: Policy, directory: Path, *, bind: bool = True
              ) -> SdkProcess:
        """Start from the SDK environment, refusing startup before app work."""
        if 'python' not in policy.data['languages']:
            raise ValueError('Python is disabled by policy')
        directory = directory.resolve()
        directory.mkdir(mode=0o700)
        owned: set[int] = set()
        process = None
        channel = None
        launch_id = os.urandom(16).hex()
        try:
            events, writer = os.pipe()
            owned.update((events, writer))
            replies, output = os.pipe()
            owned.update((replies, output))
            plan = os.open(directory / 'python-sdk-plan.json',
                           os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            owned.add(plan)
            io.write_json(plan, {'schema_version': 1, 'launch_id': launch_id,
                                'policy': policy.data, 'directory': str(directory)})
            os.lseek(plan, 0, os.SEEK_SET)
            diagnostic = io.create_file(directory / 'python-sdk.log')
            owned.add(diagnostic)
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('PYTHON', 'OTEL_', 'OTELC_',
                                                  'GH_', 'GITHUB_', 'SONAR_'))}
            process = subprocess.Popen([
                sys.executable, '-I', '-B', str(ROOT / 'otelc/python_sdk_child.py'),
                '--plan-fd', str(plan), '--events-fd', str(events),
                '--replies-fd', str(output),
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=diagnostic, cwd=ROOT, env=environment,
                pass_fds=(plan, events, output))
            for descriptor in (events, output, plan, diagnostic):
                os.close(descriptor)
                owned.remove(descriptor)
            channel = SdkChannel(writer, replies, sdk_pid=process.pid,
                                 launch_id=launch_id,
                                 policy_sha256=policy_digest(policy))
            owned.difference_update((writer, replies))
            owner = cls(process, channel, directory, policy)
            owner._ready()
            if bind:
                channel.bind(os.getpid())
            return owner
        except BaseException:
            # A caller interruption must not leave an unowned SDK process/fd.
            if channel is not None:
                channel.close()
            if process is not None:
                cls._stop_process(process)
            raise
        finally:
            for descriptor in owned:
                os.close(descriptor)

    def _ready(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                ready = private_json(self.directory / 'python-sdk-ready.json')
            except FileNotFoundError:
                ready = None
            if ready is not None:
                wire.fields(ready, {'schema_version', 'pid', 'launch_id'})
                if (type(ready['schema_version']) is not int or ready['schema_version'] != 1
                        or type(ready['pid']) is not int or ready['pid'] != self.process.pid
                        or ready['launch_id'] != self.channel.launch_id):
                    raise ValueError('Python SDK readiness identity differs')
                return
            if self.process.poll() is not None:
                raise RuntimeError('Python SDK worker exited during startup')
            time.sleep(.01)
        raise TimeoutError('Python SDK worker readiness deadline elapsed')

    def finish(self, producer: dict[str, Any] | None = None) -> dict[str, Any]:
        """Finish the source stream, then wait outside application execution."""
        if producer is not None:
            self.channel.seal(producer)
        else:
            self.channel.writer.close()
        return self.wait_report()

    def wait_report(self, *, application_pid: int | None = None
                    ) -> dict[str, Any]:
        """Require actual SDK exit and its matching final producer evidence."""
        self.process.wait(timeout=self.policy.data['export']['shutdown_timeout_ms'] / 1000 + 2)
        try:
            report = private_json(self.directory / 'python-sdk-status.json')
            expected = application_pid if application_pid is not None else self.channel.application_pid
            if (type(report.get('schema_version')) is not int
                    or report.get('schema_version') != 1
                    or report.get('kind') != 'otelc.python.sdk'
                    or type(report.get('sdk_pid')) is not int
                    or report.get('sdk_pid') != self.process.pid
                    or report.get('launch_id') != self.channel.launch_id
                    or report.get('policy_sha256') != self.channel.policy_sha256
                    or expected is not None and report.get('pid') != expected):
                raise ValueError('Python SDK final report identity differs')
            if ((report.get('pid') is not None and type(report['pid']) is not int)
                    or report.get('phase') not in ('closed', 'failed')):
                raise ValueError('invalid Python SDK final report')
            if report.get('phase') == 'closed' and self.process.returncode != 0:
                raise ValueError('Python SDK exit contradicts its final report')
            return report
        finally:
            self.channel.close()

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def abort(self) -> None:
        """Clean up this exact owned worker after failed or interrupted work."""
        self.channel.close()
        self._stop_process(self.process)
