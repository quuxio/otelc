"""Fixed owner of one prebuilt Go application and its embedded SDK."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import os
import signal
import subprocess
from types import FrameType
from typing import Any

from otelc import go_health, health, prepared_go as prepared
from otelc import python_launch_files as files

EXPECTED_FAILURES = (ValueError, OSError, RuntimeError, subprocess.SubprocessError)


class Owner:
    """Publish actual process identity and retain original exit/health separately."""

    def __init__(self, runtime: Path) -> None:
        self.directory = prepared.directory(runtime)
        self.original, self.plan, self.prepared = prepared.verify(self.directory)
        if os.getpgrp() != os.getpid():
            raise ValueError('Go supervisor requires a caller-owned new session')
        self.application: subprocess.Popen[bytes] | None = None
        self.report: dict[str, Any] | None = None
        self.failures: dict[str, str] = {}
        self.rechecked = False
        self.stopping: int | None = None
        self.owner_identity = self.owner_record()

    def owner_record(self) -> dict[str, Any]:
        return {'schema_version': 1, 'kind': 'otelc.go.supervisor', 'profile': prepared.PROFILE,
                'supervisor_pid': os.getpid(), 'parent_pid': os.getppid(), 'process_group': os.getpgrp(),
                'launch_id': self.original['launch_id'], 'request_sha256': prepared.digest(self.original),
                'plan_sha256': prepared.digest(self.plan), 'prepared_sha256': prepared.digest(self.prepared)}

    def start(self) -> None:
        environment = dict(os.environ)
        if not self.original['plain']:
            environment.update(self.prepared['config']['environment'])
        self.application = subprocess.Popen(self.plan['argv'], cwd=self.plan['cwd'], env=environment)
        files.write(self.directory / 'go-application-start.json', {
            'schema_version': 1, 'kind': 'otelc.go.application', 'profile': prepared.PROFILE,
            'launch_id': self.original['launch_id'], 'pid': self.application.pid,
            'supervisor_pid': os.getpid(), 'process_group': os.getpgid(self.application.pid),
            'plain': self.original['plain'], 'argv': self.plan['argv'], 'binary': self.plan['binary']})

    def checkpoint(self) -> None:
        """A final SDK report must belong to this actual process and fixed policy."""
        if self.original['plain']:
            if (self.directory / 'health.json').exists() or (self.directory / 'control.json').exists():
                raise ValueError('plain Go execution unexpectedly created SDK records')
            return
        report = health.read(self.directory / 'health.json')
        settings = prepared.read(self.directory / 'config.json')
        if (self.application is None or report['pid'] != self.application.pid or report['phase'] != 'final'
                or report['policy_sha256'] != settings['policy_sha256']
                or report['selection_sha256'] != self.plan['selection_sha256']):
            raise ValueError('Go final SDK checkpoint differs from the owned application/selection')
        self.report = report
        if not go_health.final_clean(report):
            raise ValueError('Go final SDK counters are not a clean drain')

    def interrupt(self, number: int, _frame: FrameType | None) -> None:
        self.stopping = number
        if self.application is not None and self.application.poll() is None:
            try:
                self.application.send_signal(number)
            except ProcessLookupError:
                pass

    def cleanup(self) -> None:
        """The outer caller remains responsible for this whole owned session."""
        if self.application is None or self.application.poll() is not None:
            return
        self.application.terminate()
        try:
            self.application.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.application.kill()
            self.application.wait(timeout=2)

    def execute(self) -> int:
        # This exclusive publication is the single-use ownership claim. A second
        # owner must not publish a failure over the first owner's final records.
        prepared.command(self.directory)
        files.write(self.directory / 'go-supervisor-start.json', self.owner_identity)
        saved = {number: signal.signal(number, self.interrupt) for number in (signal.SIGTERM, signal.SIGINT)}
        try:
            self.start()
            self.application.wait()
            prepared.verify(self.directory)
            self.rechecked = True
            self.checkpoint()
        except EXPECTED_FAILURES as error:
            self.failures['execution'] = type(error).__name__ + ': ' + str(error)
        finally:
            self.cleanup()
            for number, handler in saved.items():
                signal.signal(number, handler)
        code = self.application.returncode if self.application is not None else None
        files.write(self.directory / 'go-execution.json', {
            'schema_version': 1, 'kind': 'otelc.go.execution', 'profile': prepared.PROFILE,
            'launch_id': self.original['launch_id'], 'supervisor': self.owner_identity,
            'application_pid': self.application.pid if self.application is not None else None,
            'sdk_pid': self.application.pid if self.application is not None and not self.original['plain'] else None,
            'application_exit_code': code, 'interrupted_by': self.stopping,
            'inputs_rechecked': self.rechecked, 'sdk_health': self.report,
            'failures': self.failures, 'source_complete': False,
            'downstream_receipt': 'unavailable'})
        return prepared.supervisor_exit(code, self.stopping, self.failures)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--runtime', type=Path, required=True)
    args = parser.parse_args()
    try:
        return Owner(args.runtime).execute()
    except EXPECTED_FAILURES as error:
        print('Go prepared launch failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
