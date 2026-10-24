"""Fixed native supervisor: hold the child until an external host acknowledges it."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    if not sys.flags.isolated or not sys.flags.no_site:
        raise SystemExit('native supervisor requires isolated no-site startup')
    sys.path.insert(0, str(ROOT))
    from otelc.cli_bootstrap import install_sdk_paths
    install_sdk_paths()

from otelc import health, native_launch, prepared_native as prepared
from otelc import python_launch_files as files
from otelc.config import Policy


def execute(runtime: Path) -> int:
    """Retain both process identities and actual exit without trusting stored argv."""
    original, resolved, built, gate = prepared.verify(runtime)
    supervisor = {'schema_version': 1, 'kind': 'otelc.native.supervisor', 'pid': os.getpid(),
                  'parent_pid': os.getppid(), 'process_group': os.getpgrp(), 'session': os.getsid(0),
                  'launch_id': original['launch_id']}
    if supervisor['process_group'] != supervisor['pid'] or supervisor['session'] != supervisor['pid']:
        raise ValueError('native supervisor requires its own session')
    files.write(runtime / 'native-supervisor-start.json', supervisor)
    waiting = None
    application = None
    released, rechecked = False, False
    failures: dict[str, str] = {}
    sdk_health: dict[str, Any] | None = None
    interrupted: int | None = None

    def stopping(number: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = number
        raise InterruptedError('native supervisor was interrupted')

    previous = {number: signal.signal(number, stopping) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        policy = Policy.parse(original['policy'])
        waiting = native_launch.start_waiting(built, runtime / 'app', gate, policy=policy,
            plain=original['plain'], arguments=tuple(original['arguments']), wait_ms=original['wait_ms'])
        ready = waiting.ready()
        application = {**ready, 'kind': 'otelc.native.application', 'supervisor_pid': os.getpid(),
                       'launch_id': original['launch_id'], 'image': resolved['image'],
                       'phase': 'waiting_exec', 'source_complete': False}
        if application['parent_pid'] != os.getpid():
            raise ValueError('native child is not parented by the owned supervisor')
        files.write(runtime / 'native-application-start.json', application)
        deadline = time.monotonic() + original['wait_ms'] / 1000
        release = runtime / 'native-host-release.json'
        value = None
        while value is None:
            if waiting.process.poll() is not None:
                raise ValueError('native gate ended without host release')
            if time.monotonic() >= deadline:
                raise ValueError('native host release deadline expired')
            try:
                value, _ = files.read(release)
            except (FileNotFoundError, files.PublicationPending):
                time.sleep(.005)
        expected = {'schema_version': 1, 'kind': 'otelc.native.host-release',
                    'launch_id': original['launch_id'], 'pid': waiting.process.pid, 'supervisor_pid': os.getpid()}
        if not prepared.same(value, expected):
            raise ValueError('native host release does not match the owned waiting process')
        prepared.verify(runtime)
        waiting.release()
        released = True
        waiting.process.wait()
        if not original['plain']:
            sdk_health = health.read(runtime / 'app/health.json')
            prepared.validate_health(sdk_health, built=built, pid=waiting.process.pid, policy=policy.data)
        prepared.verify(runtime)
        rechecked = True
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        failures['supervisor'] = type(error).__name__
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if waiting is not None:
            try:
                waiting.cancel()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                failures['cleanup'] = type(error).__name__
    code = waiting.process.returncode if waiting is not None else None
    exit_code = (128 - code if code < 0 else code) if released and code else 1 if failures else 0
    if interrupted is not None:
        exit_code = 128 + interrupted
    final = {'schema_version': 1, 'kind': 'otelc.native.execution', 'profile': prepared.PROFILE,
             'request_sha256': native_launch.digest(original), 'plan_sha256': native_launch.digest(resolved),
             'supervisor': supervisor, 'application': application, 'released': released,
             'process_exit_code': code, 'supervisor_exit_code': exit_code,
             'exec_verified': False, 'sdk_health': sdk_health, 'inputs_rechecked': rechecked,
             'failures': failures, 'source_complete': False}
    files.write(runtime / 'native-execution.json', final)
    return exit_code


def main() -> int:
    """Accept one runtime path; metadata never supplies a Python module or argv."""
    if len(sys.argv) != 2:
        raise ValueError('native supervisor requires one runtime directory')
    return execute(Path(sys.argv[1]).resolve(strict=True))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print('quux-otelc native supervisor: ' + str(error), file=sys.stderr)
        raise SystemExit(1) from error
