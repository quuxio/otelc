"""Pause an owned native process before exec, without loading application code."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time
from typing import Any

from otelc import file_inputs, llvm_backend
from otelc.prepared_python import stop_group
from otelc.python_sdk_io import decode

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'adapters/native/gate/ExecGate.c'
FIELDS = {'schema_version', 'kind', 'pid', 'parent_pid', 'process_group', 'session'}


def identity(path: Path) -> dict[str, Any]:
    """Keep caller-held bytes and file facts across one regular-file read."""
    before = path.lstat()
    value, _ = file_inputs.checked_file(path, nofollow=True, include_stat=True)
    after = path.lstat()
    facts = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size,
                          item.st_mtime_ns, item.st_ctime_ns, item.st_nlink, item.st_uid, item.st_gid)
    if facts(before) != facts(after):
        raise ValueError('native gate input changed while reading')
    value.update(mode=after.st_mode, ctime_ns=after.st_ctime_ns, links=after.st_nlink,
                 uid=after.st_uid, gid=after.st_gid)
    return value


@dataclass(frozen=True)
class Product:
    """Bind fixed gate/compiler/source inputs independently of edited metadata."""

    binding: str

    def metadata(self) -> dict[str, Any]:
        """Return a fresh primitive view without mutable aliases."""
        return json.loads(self.binding)

    def verify(self) -> None:
        """Recheck bytes and facts without executing or rebuilding tooling."""
        value = self.metadata()
        for field in ('source', 'compiler', 'executable'):
            if identity(Path(value[field]['path'])) != value[field]:
                raise ValueError('native gate input changed: ' + field)


def build(output: Path, *, coverage: bool = False) -> Product:
    """Compile one fixed C11 helper using the matched LLVM 22 C compiler."""
    tools = llvm_backend.discover()
    compiler = (Path(tools['directory']) / 'clang').resolve(strict=True)
    before = [identity(path) for path in (SOURCE, compiler)]
    output = output.resolve()
    output.mkdir(mode=0o700)
    executable = output / 'otelc-exec-gate'
    flags = ['-fprofile-instr-generate', '-fcoverage-mapping', '-DOTELC_GATE_COVERAGE=1'] if coverage else []
    command = [str(compiler), '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
               *flags, str(SOURCE), '-o', str(executable)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=120)
    for name, raw in (('stdout', result.stdout), ('stderr', result.stderr)):
        (output / ('compile.' + name)).write_bytes(raw)
    if result.returncode:
        raise ValueError('native gate compilation failed; inspect compile.stderr')
    if before != [identity(path) for path in (SOURCE, compiler)] or not llvm_backend.unchanged(tools):
        raise ValueError('native gate compiler or source changed during compilation')
    value = {'schema_version': 1, 'kind': 'otelc.native.exec-gate', 'source': before[0],
             'compiler': before[1], 'executable': identity(executable), 'coverage': coverage,
             'command': command, 'source_complete': False}
    (output / 'gate.json').write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    (output / 'compile_commands.json').write_text(json.dumps([{
        'directory': str(ROOT), 'file': str(SOURCE), 'arguments': command}], indent=2) + '\n')
    return Product(json.dumps(value, sort_keys=True))


class Waiting:
    """One single-use host release for a child that has not executed its image."""

    def __init__(self, process: subprocess.Popen[bytes], events: int, release: int,
                 product: Product, target: dict[str, Any], timeout: int):
        self.process, self.product, self.target = process, product, json.dumps(target, sort_keys=True)
        self.events, self.release_fd = events, release
        self.deadline = time.monotonic() + timeout / 1000
        self._ready: dict[str, Any] | None = None
        self._released = False

    def ready(self) -> dict[str, Any]:
        """Observe the pre-exec child; this does not acknowledge or start it."""
        if self._released:
            raise ValueError('native gate was already released')
        self.product.verify()
        target = json.loads(self.target)
        if identity(Path(target['path'])) != target:
            raise ValueError('native target changed before release')
        if self._ready is None:
            self._ready = self._read_ready()
        value = self._ready
        expected = {'schema_version': 1, 'kind': 'otelc.native.exec-ready',
                    'pid': self.process.pid, 'parent_pid': os.getpid(),
                    'process_group': self.process.pid, 'session': self.process.pid}
        if (set(value) != FIELDS or json.dumps(value, sort_keys=True) != json.dumps(expected, sort_keys=True)
                or self.process.poll() is not None or os.getpgid(self.process.pid) != self.process.pid
                or os.getsid(self.process.pid) != self.process.pid):
            raise ValueError('native gate does not identify the owned waiting process')
        return dict(value)

    def _read_ready(self) -> dict[str, Any]:
        """Read one bounded readiness frame within the original release budget."""
        raw = b''
        while b'\n' not in raw and len(raw) <= 1024:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.events], [], [], remaining)[0]:
                raise ValueError('native gate readiness timed out')
            piece = os.read(self.events, 1025 - len(raw))
            if not piece:
                raise ValueError('native gate ended before readiness')
            raw += piece
        if len(raw) > 1024 or not raw.endswith(b'\n'):
            raise ValueError('native gate readiness exceeds its framing bound')
        return decode(raw)

    def release(self) -> None:
        """Recheck the held child and inputs immediately before one explicit ack."""
        if self._ready is None:
            raise ValueError('verify native gate readiness before release')
        self.ready()
        if time.monotonic() >= self.deadline:
            raise ValueError('native gate release deadline expired')
        try:
            if os.write(self.release_fd, b'G') != 1:
                raise ValueError('native gate release was incomplete')
            self._released = True
        finally:
            os.close(self.release_fd)
            self.release_fd = -1
            os.close(self.events)
            self.events = -1

    def cancel(self) -> None:
        """Close the release capability and reap only this owned process group."""
        for name in ('release_fd', 'events'):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)
        if not self._released:
            # This fixed helper has not exec'd or forked application code.
            # Closing its private pipe is cancellation; reap it before any
            # process-group probe, including a vanished macOS zombie group.
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        else:
            self.process.poll()
            stop_group(self.process)


def start(product: Product, command: tuple[str, ...], *, cwd: Path,
          environment: dict[str, str] | None = None, pass_fds: tuple[int, ...] = (),
          wait_ms: int = 30000, stdin: Any = None, stdout: Any = None,
          stderr: Any = None) -> Waiting:
    """Start only the fixed helper; the host must separately release the image."""
    if (type(product) is not Product or type(command) is not tuple or not command
            or any(type(value) is not str or '\0' in value for value in command)
            or not Path(command[0]).is_absolute() or type(wait_ms) is not int or not 1 <= wait_ms <= 300000):
        raise ValueError('invalid native gate command or wait budget')
    product.verify()
    target = identity(Path(command[0]))
    target_environment = dict(os.environ) if environment is None else dict(environment)
    if any(type(key) is not str or type(value) is not str or not key or '=' in key
           or '\0' in key or '\0' in value for key, value in target_environment.items()):
        raise ValueError('invalid native target environment')
    entries = [os.fsencode(key + '=' + value) + b'\0' for key, value in target_environment.items()]
    if len(entries) > 65536 or sum(map(len, entries)) > 4 * 1024 * 1024:
        raise ValueError('native target environment exceeds its bound')
    # Loader variables apply only after the host acknowledges the child. Keep
    # environment values in an anonymous inherited file, never in retained logs.
    helper_environment = {'PATH': os.defpath, 'LC_ALL': 'C'}
    if product.metadata()['coverage']:
        helper_environment['LLVM_PROFILE_FILE'] = target_environment.get(
            'LLVM_PROFILE_FILE', str(Path(product.metadata()['executable']['path']).parent / '%m-%p.profraw'))
    with tempfile.TemporaryFile() as plan:
        plan.write(b''.join(entries)); plan.flush(); plan.seek(0)
        events, writer = os.pipe()
        try:
            reader, release = os.pipe()
        except BaseException:
            os.close(events); os.close(writer)
            raise
        try:
            args = [product.metadata()['executable']['path'], str(writer), str(reader), str(wait_ms),
                    str(plan.fileno()), *command]
            process = subprocess.Popen(args, cwd=cwd, env=helper_environment,
                pass_fds=(*pass_fds, writer, reader, plan.fileno()), start_new_session=True,
                stdin=stdin, stdout=stdout, stderr=stderr)
        except BaseException:
            os.close(events); os.close(release)
            raise
        finally:
            os.close(writer); os.close(reader)
    return Waiting(process, events, release, product, target, wait_ms)
