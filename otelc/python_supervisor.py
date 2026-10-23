"""Fixed owner of one native application, its journal and an independent SDK."""

from __future__ import annotations

import sys
from pathlib import Path

# The fixed -I/-S/-B script neither searches app PYTHONPATH nor runs site hooks.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import os
import signal
import subprocess
from types import FrameType
from typing import Any

from otelc.config import Policy
from otelc.file_inputs import identity
from otelc import prepared_python as prepared
from otelc import python as frontend
from otelc import python_input_journal as inputs
from otelc import python_launch_files as files
from otelc import python_recipe as recipe
from otelc.python_sdk_process import SdkProcess

EXPECTED_FAILURES = (ValueError, OSError, RuntimeError, subprocess.SubprocessError)


class Owner:
    """Keep actual identities and unavailable evidence separate from clean counters."""

    def __init__(self, runtime: Path) -> None:
        self.directory = files.private_directory(runtime)
        self.environment = dict(os.environ)
        self.original, self.plan, self.prepared = prepared.verify(
            self.directory, environment=self.environment)
        self.policy = Policy.parse(self.original['policy'])
        self.application: subprocess.Popen[bytes] | None = None
        self.sdk: SdkProcess | None = None
        self.sdk_report: dict[str, Any] | None = None
        self.bootstrap_descriptor: int | None = None
        self.bootstrap_plan: dict[str, Any] | None = None
        self.entry: dict[str, Any] = {'phase': 'unavailable', 'python_finalized': None}
        self.failures: dict[str, str] = {}
        self.input_recheck = False
        self.bootstrap_recheck: bool | None = None
        self.stopping: int | None = None

    def execute(self) -> int:
        """Publish final ownership only after actual app/SDK completion and rechecks."""
        try:
            files.write(self.directory / 'python-supervisor-start.json', self.start_record())
            self.start_application()
            self.application.wait()
            self.finish_sdk()
            self.check_entry()
            self.recheck_inputs()
        except EXPECTED_FAILURES as error:
            self.failures['supervisor'] = type(error).__name__
        finally:
            self.cleanup()
        self.publish_final()
        code = self.application.returncode if self.application is not None else None
        if code:
            return code if code > 0 else 128 - code
        if self.stopping is not None:
            return 128 + self.stopping
        return 1 if self.failures else 0

    def start_record(self) -> dict[str, Any]:
        return {'schema_version': 1, 'kind': 'otelc.python.supervisor',
                'supervisor_pid': os.getpid(), 'parent_pid': os.getppid(),
                'process_group': os.getpgrp(),
                'request_sha256': prepared.digest(self.original),
                'plan_sha256': prepared.digest(self.plan),
                'prepared_sha256': prepared.digest(self.prepared)}

    def start_application(self) -> None:
        """Pass only owned descriptors; parent copies close before waiting for EOF."""
        plan_fd = None
        journal_fd = None
        try:
            environment = self.environment
            inherited = []
            if not self.original['plain']:
                self.sdk = SdkProcess.start(self.policy, self.directory / 'sdk', bind=False)
                events, replies = self.sdk.channel.descriptors
                plan_fd, environment = frontend.prepare(self.policy, self.directory, self.sdk,
                                                        Path(self.plan['cwd']))
                self.bootstrap_descriptor = plan_fd
                self.bootstrap_plan = {'schema_version': 1, 'policy': self.policy.data,
                    'root': self.plan['cwd'], 'directory': str(self.directory),
                    'events_fd': events, 'replies_fd': replies, 'sdk_pid': self.sdk.process.pid,
                    'launch_id': self.sdk.channel.launch_id,
                    'policy_sha256': self.sdk.channel.policy_sha256}
                inherited.extend((events, replies, plan_fd))
            journal_fd = os.open(self.directory / 'python-inputs.journal',
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            inherited.append(journal_fd)
            invocation = recipe.native_command(self.original, prepared.native_tool(), journal_fd)
            self.application = subprocess.Popen(invocation, cwd=self.plan['cwd'],
                env=environment, pass_fds=tuple(inherited))
            files.write(self.directory / 'python-application-start.json', {
                **self.start_record(), 'kind': 'otelc.python.application',
                'application_pid': self.application.pid,
                'sdk_pid': self.sdk.process.pid if self.sdk is not None else None,
                'sdk_ready': self.sdk is not None,
                'sdk_launch_id': self.sdk.channel.launch_id if self.sdk is not None else None,
                'native_command': invocation})
        finally:
            for descriptor in (plan_fd, journal_fd):
                if descriptor is not None:
                    os.close(descriptor)
            if self.sdk is not None:
                self.sdk.channel.close()

    def finish_sdk(self) -> None:
        """A final SDK phase must come from the matching worker after actual exit."""
        if self.sdk is None:
            return
        try:
            self.sdk_report = self.sdk.wait_report(application_pid=self.application.pid)
            if self.sdk_report['phase'] != 'closed':
                self.failures['sdk'] = 'failed'
        except EXPECTED_FAILURES:
            self.failures['sdk'] = 'unverified'
        finally:
            self.sdk.abort()

    def check_entry(self) -> None:
        """Bind consumed script bytes to prepared input; modules stay unavailable."""
        path = self.directory / 'python-inputs.journal'
        try:
            journal = inputs.read(path, expected_pid=self.application.pid)
            self.entry = {'phase': 'unavailable', 'python_finalized': journal.python_finalized,
                'capture_failures': journal.capture_failures, 'journal_failures': journal.journal_failures,
                'journal': identity(path), 'sources': [
                    {'path_hex': source.path.hex(), 'sha256': source.sha256,
                     'device': source.device, 'inode': source.inode, 'mtime_ns': source.mtime_ns}
                    for source in journal.sources], 'imports': [
                    {'path_hex': source.path.hex(), 'sha256': source.sha256,
                     'device': source.device, 'inode': source.inode, 'mtime_ns': source.mtime_ns}
                    for source in journal.imports], 'codes': [
                    {'id': code.identity, 'path_hex': code.path.hex(), 'sha256': code.sha256,
                     'offset': code.offset, 'python_version': code.python_version,
                     'descriptor_version': code.descriptor_version} for code in journal.codes]}
            if self.original['module'] is None:
                source = journal.sources[0] if len(journal.sources) == 1 else None
                if (not journal.complete or source is None
                        or source.sha256 != self.prepared['entry']['sha256']
                        or Path(os.fsdecode(source.path)).resolve(strict=True)
                        != Path(self.original['paths']['entry']['resolved'])):
                    raise ValueError('native entry input differs or is incomplete')
                self.entry['phase'] = 'complete'
            elif not journal.python_finalized or journal.capture_failures or journal.journal_failures:
                raise ValueError('native module finalization is incomplete')
        except EXPECTED_FAILURES:
            self.entry['phase'] = 'failed'
            self.failures['entry'] = 'unverified'

    def recheck_inputs(self) -> None:
        """Run after SDK exit, preserving original environment before hook injection."""
        try:
            original, plan, manifest = prepared.verify(self.directory, environment=self.environment)
            if (original, plan, manifest) != (self.original, self.plan, self.prepared):
                raise ValueError('Python original prepared records changed')
            self.input_recheck = True
        except EXPECTED_FAILURES:
            self.failures['inputs'] = 'changed_or_unavailable'
        if self.sdk is not None:
            expected = frontend.startup_text(self.directory / 'bootstrap', self.directory,
                self.bootstrap_descriptor, self.environment.get('PYTHONPATH')).encode()
            try:
                plan, _ = files.read(self.directory / 'python-plan.json')
                actual = files.read_bytes(self.directory / 'bootstrap/sitecustomize.py')
                if plan != self.bootstrap_plan or actual != expected:
                    raise ValueError('Python generated startup inputs changed')
                self.bootstrap_recheck = True
            except EXPECTED_FAILURES:
                self.bootstrap_recheck = False
                self.failures['bootstrap'] = 'changed_or_unavailable'

    def cleanup(self) -> None:
        """Terminate/reap only this owner's actual app and SDK after failure."""
        if self.application is not None:
            SdkProcess._stop_process(self.application)
        if self.sdk is not None:
            self.sdk.abort()

    def publish_final(self) -> None:
        """Closed describes this lifecycle; full source and downstream health stay open."""
        result = {**self.start_record(), 'kind': 'otelc.python.execution',
            'phase': 'failed' if self.failures else 'closed',
            'application': None if self.application is None else {
                'pid': self.application.pid, 'returncode': self.application.returncode},
            'sdk': self.sdk_identity(), 'plain': self.original['plain'], 'entry_evidence': self.entry,
            'input_recheck': self.input_recheck, 'bootstrap_recheck': self.bootstrap_recheck,
            'source_complete': False, 'failures': self.failures}
        try:
            files.write(self.directory / 'python-execution.json', result)
        except EXPECTED_FAILURES:
            self.failures['final_record'] = 'unavailable'
        if self.failures:
            try:
                files.write(self.directory / 'python-supervisor-error.json', {
                    'schema_version': 1, 'failures': self.failures,
                    'message': 'Python launch completion or drain remains unverified'})
            except EXPECTED_FAILURES:
                pass

    def sdk_identity(self) -> dict[str, Any] | None:
        """Retain actual failed-start PIDs/exits; unavailable is never a zero PID."""
        if self.sdk is not None:
            return {'pid': self.sdk.process.pid, 'returncode': self.sdk.process.returncode,
                    'launch_id': self.sdk.channel.launch_id,
                    'phase': self.sdk_report['phase'] if self.sdk_report is not None else None}
        if self.original['plain']:
            return None
        try:
            failed, _ = files.read(self.directory / 'sdk/python-sdk-startup-failed.json')
            if (failed.get('schema_version') != 1 or failed.get('parent_pid') != os.getpid()
                    or type(failed.get('pid')) is not int or failed['pid'] <= 0
                    or type(failed.get('returncode')) is not int):
                raise ValueError('failed SDK startup identity is unavailable')
            return {'pid': failed['pid'], 'returncode': failed['returncode'],
                    'launch_id': failed.get('launch_id'), 'phase': 'startup_failed'}
        except EXPECTED_FAILURES:
            return {'pid': None, 'returncode': None, 'launch_id': None,
                    'phase': 'startup_unverified'}

    def interrupt(self, number: int, _frame: FrameType | None) -> None:
        """A second TERM cannot interrupt cleanup; the external owner can still KILL."""
        if self.stopping is None:
            self.stopping = number
            raise InterruptedError('Python supervisor interrupted')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.getpgrp() != os.getpid():
            os.setpgid(0, 0)
        owner = Owner(args.runtime)
        signal.signal(signal.SIGTERM, owner.interrupt)
        signal.signal(signal.SIGINT, owner.interrupt)
        return owner.execute()
    except EXPECTED_FAILURES as error:
        # Refused preparation never starts app code or prints into app channels.
        try:
            files.write(files.private_directory(args.runtime) / 'python-supervisor-error.json',
                        {'schema_version': 1, 'phase': 'before_application',
                         'error_type': type(error).__name__, 'reason': str(error)[:512]})
        except EXPECTED_FAILURES:
            pass
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
