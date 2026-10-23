"""Benchmark the public prepared recipe without confusing app and owner PIDs."""

from __future__ import annotations

import os
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, TextIO

from otelc import benchmark, prepared_python as prepared
from otelc import python_launch_files as files
from otelc import python_input_journal as inputs
from otelc import python_sdk_protocol as wire
from otelc.config import Policy
from otelc.file_inputs import identity
from otelc.python_sdk_process import policy_digest, private_json


@dataclass
class Application:
    """Actual app PID and pipes, with wait/poll covering its supervisor lifecycle."""

    owner: subprocess.Popen[str]
    pid: int
    sdk_pid: int | None
    start: dict[str, Any]
    sdk_report: dict[str, Any] | None = None
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def supervisor_pid(self) -> int:
        return self.owner.pid

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
        self.owner.kill()


def bindings(runtime: Path, owner: subprocess.Popen[str]) -> dict[str, Any]:
    """Bind a receipt to the independently checked request and the actual owner."""
    original, plan, manifest = prepared.verify(runtime)
    return {'supervisor_pid': owner.pid, 'parent_pid': os.getpid(),
            'process_group': owner.pid, 'request_sha256': prepared.digest(original),
            'plan_sha256': prepared.digest(plan), 'prepared_sha256': prepared.digest(manifest)}


def matches(value: dict[str, Any], expected: dict[str, Any]) -> bool:
    """JSON floats and booleans must not substitute for integer ownership IDs."""
    return all(type(value.get(key)) is type(item) and value[key] == item
               for key, item in expected.items())


def start(owner: subprocess.Popen[str], runtime: Path, *, plain: bool,
          timeout: float = 20.) -> Application:
    """Wait for atomically published ownership, never infer an app PID from pipes."""
    expected = bindings(runtime, owner)
    deadline = time.monotonic() + timeout
    while True:
        try:
            value, _ = files.read(runtime / 'python-application-start.json')
            break
        except FileNotFoundError:
            if owner.poll() is not None:
                raise RuntimeError('prepared Python benchmark exited before application readiness')
            if time.monotonic() >= deadline:
                raise TimeoutError('prepared Python benchmark ownership is unavailable')
            time.sleep(.01)
    wire.fields(value, set(expected) | {'schema_version', 'kind', 'application_pid',
                'sdk_pid', 'sdk_ready', 'sdk_launch_id', 'native_command'})
    if (type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('kind') != 'otelc.python.application'
            or not matches(value, expected)
            or value.get('sdk_ready') is not (not plain)):
        raise ValueError('prepared Python benchmark startup identity differs')
    pid, sdk_pid = value.get('application_pid'), value.get('sdk_pid')
    if (type(pid) is not int or pid <= 0 or pid == owner.pid
            or (sdk_pid is not None if plain else
                type(sdk_pid) is not int or sdk_pid <= 0 or sdk_pid in (pid, owner.pid))):
        raise ValueError('prepared Python benchmark process identities are unavailable')
    launch_id = value.get('sdk_launch_id')
    if (plain and launch_id is not None or not plain
            and (type(launch_id) is not str or not re.fullmatch('[0-9a-f]{32}', launch_id))):
        raise ValueError('prepared Python benchmark SDK launch identity is unavailable')
    if owner.poll() is not None:
        raise RuntimeError('prepared Python benchmark owner exited during startup')
    for child in (pid,) if plain else (pid, sdk_pid):
        if os.getpgid(child) != owner.pid:
            raise ValueError('prepared Python benchmark child is outside its owned group')
    return Application(owner, pid, sdk_pid, value)


def completed(application: Application, runtime: Path, policy: Policy, *, plain: bool) -> None:
    """Require closed actual ownership and stable inputs before any publication."""
    value, _ = files.read(runtime / 'python-execution.json')
    expected = bindings(runtime, application.owner)
    wire.fields(value, set(expected) | {'schema_version', 'kind', 'phase', 'plain',
        'source_complete', 'failures', 'input_recheck', 'bootstrap_recheck',
        'application', 'sdk', 'entry_evidence'})
    app = value.get('application')
    if (type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('kind') != 'otelc.python.execution'
            or value.get('phase') != 'closed' or value.get('plain') is not plain
            or value.get('source_complete') is not False or value.get('failures') != {}
            or value.get('input_recheck') is not True
            or value.get('bootstrap_recheck') is not (None if plain else True)
            or not matches(value, expected)
            or app != {'pid': application.pid, 'returncode': 0}
            or type(app['returncode']) is not int or type(app['pid']) is not int):
        raise ValueError('prepared Python benchmark completion is not verified')
    entry = value.get('entry_evidence')
    if (type(entry) is not dict or entry.get('phase') != 'complete'
            or entry.get('python_finalized') is not True
            or any(type(entry.get(key)) is not int or entry[key] != 0
                   for key in ('capture_failures', 'journal_failures'))):
        raise ValueError('prepared Python benchmark entry evidence is incomplete')
    journal_path = runtime / 'python-inputs.journal'
    journal = inputs.read(journal_path, expected_pid=application.pid)
    source = prepared.ROOT / 'examples/python/BenchmarkApp.py'
    if (not journal.complete or len(journal.sources) != 1
            or journal.sources[0].sha256 != identity(source)['sha256']
            or Path(os.fsdecode(journal.sources[0].path)).resolve(strict=True) != source
            or entry.get('journal') != identity(journal_path)):
        raise ValueError('prepared Python benchmark consumed entry differs')
    sdk = value.get('sdk')
    if plain:
        if sdk is not None:
            raise ValueError('plain Python benchmark unexpectedly has an SDK')
        retain(application, runtime, plain=plain)
        return
    if (type(sdk) is not dict or type(sdk.get('pid')) is not int
            or sdk['pid'] != application.sdk_pid
            or type(sdk.get('returncode')) is not int or sdk['returncode'] != 0
            or sdk.get('phase') != 'closed'
            or sdk.get('launch_id') != application.start.get('sdk_launch_id')):
        raise ValueError('prepared Python benchmark SDK ownership differs')
    report = private_json(runtime / 'sdk/python-sdk-status.json')
    if (type(report.get('schema_version')) is not int or report['schema_version'] != 1
            or report.get('kind') != 'otelc.python.sdk'
            or type(report.get('pid')) is not int or type(report.get('sdk_pid')) is not int
            or report['pid'] != application.pid or report['sdk_pid'] != application.sdk_pid
            or report.get('launch_id') != sdk['launch_id']
            or report.get('policy_sha256') != policy_digest(policy)):
        raise ValueError('prepared Python benchmark final SDK identity differs')
    application.sdk_report = report
    retain(application, runtime, plain=plain)


def retain(application: Application, runtime: Path, *, plain: bool) -> None:
    """Keep final file identities for a second check after both lanes complete."""
    names = (prepared.REQUEST, prepared.PLAN, prepared.PREPARED,
             'python-application-start.json', 'python-execution.json', 'python-inputs.journal')
    if not plain:
        names += ('sdk/python-sdk-status.json',)
    application.evidence = {name: identity(runtime / name) for name in names}


def recheck(application: Application) -> None:
    """Do not publish evidence changed during the other benchmark lane."""
    for value in application.evidence.values():
        if identity(Path(value['path'])) != value:
            raise ValueError('prepared Python benchmark retained evidence changed')


@contextmanager
def application(policy: Policy, runtime: Path, *, plain: bool) -> Iterator[Application]:
    """Own the same public supervisor/app/SDK recipe in both benchmark lanes."""
    command = prepared.command(policy, runtime, root=prepared.ROOT,
        entry=prepared.ROOT / 'examples/python/BenchmarkApp.py', plain=plain)
    with (runtime.parent / ('plain.stderr' if plain else 'instrumented.stderr')).open('w') as errors:
        owner = subprocess.Popen(command, cwd=prepared.ROOT, start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
        process = None
        failure = None
        try:
            process = start(owner, runtime, plain=plain)
            files.write(runtime / 'process.json', {'driver_pid': os.getpid(),
                'supervisor_pid': owner.pid, 'application_pid': process.pid,
                'sdk_pid': process.sdk_pid, 'supervisor_command': command})
            yield process
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                try:
                    if process is not None:
                        benchmark.stop(process)
                finally:
                    prepared.stop_group(owner)
            except BaseException as error:
                if failure is None:
                    raise
                failure.add_note('Prepared benchmark cleanup also failed: ' + type(error).__name__)
            finally:
                for stream in (owner.stdin, owner.stdout):
                    if stream is not None:
                        stream.close()
        completed(process, runtime, policy, plain=plain)
