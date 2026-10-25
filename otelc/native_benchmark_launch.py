"""Direct stock native baseline and public owned instrumented benchmark execution."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any, Iterator, TextIO

from otelc import benchmark, native_build, native_gate, native_launch, prepared_native as prepared
from otelc.config import Policy


def parent(pid: int) -> int | None:
    """Query the kernel independently before releasing or signalling a child."""
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'ppid='],
                            capture_output=True, text=True, timeout=3)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode or not result.stdout.strip().isdigit():
        raise ValueError('native benchmark kernel parent is unavailable')
    return int(result.stdout.strip())


@dataclass
class Application:
    """Pipes belong to the owner; CPU and latency identify the actual application."""

    owner: subprocess.Popen[str]
    pid: int
    supervisor_pid: int | None
    created_at_monotonic_ns: int
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    first_response: dict[str, Any] | None = None
    sdk_report: dict[str, Any] | None = None

    @property
    def stdin(self) -> TextIO | None:
        return self.owner.stdin

    @property
    def stdout(self) -> TextIO | None:
        return self.owner.stdout

    def poll(self) -> int | None:
        return self.owner.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.owner.wait(timeout=timeout)

    def kill(self) -> None:
        """Give the owner time to reap its separate application group first."""
        self.owner.terminate()
        try:
            self.owner.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if self.supervisor_pid is not None and parent(self.pid) == self.owner.pid:
                try:
                    if os.getpgid(self.pid) == self.pid:
                        os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.owner.kill()
            self.owner.wait(timeout=5)


def start(owner: subprocess.Popen[str], runtime: Path) -> int:
    """Check the waiting child against the fixed plan and actual OS ownership."""
    original, resolved, _, _ = prepared.verify(runtime)
    deadline = time.monotonic() + 20
    while True:
        if owner.poll() is not None:
            raise ValueError('native benchmark owner ended before readiness')
        try:
            value, _ = prepared.files.read(runtime / 'native-application-start.json')
            break
        except (FileNotFoundError, prepared.files.PublicationPending):
            if time.monotonic() >= deadline:
                raise TimeoutError('native benchmark ownership deadline expired')
            time.sleep(.01)
    prepared.application(value, original, resolved, owner.pid)
    pid = value['pid']
    if (parent(pid) != owner.pid or os.getpgid(pid) != pid or os.getsid(pid) != pid
            or os.getpgid(owner.pid) != owner.pid or os.getsid(owner.pid) != owner.pid):
        raise ValueError('native benchmark actual process ownership differs')
    prepared.release(runtime, pid=pid, supervisor_pid=owner.pid)
    return pid


def environment(output: Path) -> dict[str, str]:
    """Keep both lanes free of ambient telemetry preload/bootstrap injection."""
    result = {key: value for key, value in os.environ.items()
              if not key.startswith(('OTEL_', 'OTELC_')) and key not in
              ('LD_PRELOAD', 'LD_AUDIT', 'DYLD_INSERT_LIBRARIES', 'DYLD_FORCE_FLAT_NAMESPACE')}
    result['LLVM_PROFILE_FILE'] = str(output / 'benchmark-sdk-%p.profraw')
    return result


def retain(process: Application, runtime: Path, *, plain: bool) -> None:
    """Seal the actual launch/completion evidence for the other lane's recheck."""
    names = ['native-benchmark-first-response.json', 'stderr.log']
    if plain:
        names += ['native-benchmark-start.json', 'native-benchmark-execution.json']
    else:
        names += [prepared.REQUEST, prepared.PLAN, prepared.PREPARED,
                  'native-supervisor-start.json', 'native-application-start.json',
                  'native-host-release.json', 'native-execution.json', 'app/health.json']
    process.evidence = {name: native_gate.identity(runtime / name) for name in names}


def recheck(process: Application) -> None:
    """A later lane cannot replace earlier evidence and retain a successful report."""
    for row in process.evidence.values():
        if native_gate.identity(Path(row['path'])) != row:
            raise ValueError('native benchmark retained evidence changed')


@contextmanager
def application(policy: Policy, built: native_launch.Build, runtime: Path, *, plain: bool
                ) -> Iterator[Application]:
    """Always close the owned application and read any available final SDK record."""
    native_launch.verify(built)
    if plain:
        runtime.mkdir(mode=0o700)
        command = [str(built.directory / 'plain')]
    else:
        prepared.prepare(policy, runtime, build_directory=built.directory)
        command = prepared.command(runtime)
    process = None
    with (runtime / 'stderr.log').open('x') as errors:
        created = time.monotonic_ns()
        owner = subprocess.Popen(command, cwd=built.manifest()['root'], env=environment(runtime),
            start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
        process = Application(owner, owner.pid, None, created)
        try:
            if plain:
                if parent(owner.pid) != os.getpid() or os.getpgid(owner.pid) != owner.pid:
                    raise ValueError('stock native benchmark ownership differs')
                prepared.files.write(runtime / 'native-benchmark-start.json', {
                    'pid': owner.pid, 'parent_pid': os.getpid(), 'argv': command,
                    'image': native_gate.identity(built.directory / 'plain'), 'sdk_pid': None})
            else:
                process.pid = start(owner, runtime)
                process.supervisor_pid = owner.pid
            yield process
        finally:
            try:
                benchmark.stop(process)
            finally:
                if plain:
                    prepared.files.write(runtime / 'native-benchmark-execution.json', {
                        'pid': owner.pid, 'returncode': owner.returncode, 'sdk_pid': None})
                elif process.supervisor_pid is not None:
                    final = prepared.execution(runtime, supervisor_pid=owner.pid,
                        parent_pid=os.getpid(), returncode=owner.returncode)
                    process.sdk_report = final['sdk_health']
                    if final['failures'] or not final['inputs_rechecked']:
                        raise ValueError('native benchmark completion is incomplete')
                native_launch.verify(built)
