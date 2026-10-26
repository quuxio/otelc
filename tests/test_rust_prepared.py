"""Literal prepared Rust controls; genuine SDK execution is tested separately."""
from __future__ import annotations

from dataclasses import replace
import json
import io
import os
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from otelc import native_gate, prepared_rust as prepared, rust_build, rust_launch, rust_supervisor
from otelc import python_launch_files as files
from otelc import __main__ as cli
import test_rust_owned_build as fixtures


class RustPreparedTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = fixtures.RustOwnedBuildTests('test_both_source_profiles_hold_original_records_and_return_fresh_views')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.base, self.policy = fixture.base, fixture.policy
        fixture.metadata['selection_sha256'] = 'a' * 64
        fixture.source_record.write_text(json.dumps(fixture.metadata))
        fixture.source = replace(fixture.source, encoded=fixture.source_record.read_text())
        for name in ('plain', 'instrumented'):
            (fixture.source_dir / name).write_bytes(b'literal image, never executed')
        self.owned = fixture.build()
        reader = patch.object(prepared.rust_owned_build, 'read', return_value=self.owned)
        reader.start()
        self.addCleanup(reader.stop)
        self.runtime = self.base / 'runtime'

    def prepare(self, *, plain: bool = True, wait_ms: int = 30000) -> dict:
        return prepared.prepare(self.policy, self.runtime, build_directory=self.owned.directory,
                                arguments=('literal $arg; *',), plain=plain, wait_ms=wait_ms)

    def records(self) -> tuple[dict, dict]:
        original, resolved, _, _ = prepared.verify(self.runtime)
        return original, resolved

    def starts(self) -> tuple[dict, dict]:
        original, resolved = self.records()
        start = {'schema_version': 1, 'kind': 'otelc.rust.supervisor', 'pid': 900002,
                 'parent_pid': 900003, 'process_group': 900002, 'session': 900002,
                 'launch_id': original['launch_id']}
        app = {'schema_version': 1, 'kind': 'otelc.rust.application', 'pid': 900001,
               'parent_pid': 900002, 'process_group': 900001, 'session': 900001,
               'supervisor_pid': 900002, 'launch_id': original['launch_id'],
               'image': resolved['image'], 'phase': 'waiting_exec', 'source_complete': False}
        files.write(self.runtime / 'rust-supervisor-start.json', start)
        files.write(self.runtime / 'rust-application-start.json', app)
        return start, app

    def final(self, *, code: int = 0) -> dict:
        original, resolved = self.records()
        supervisor, app = self.starts()
        prepared.release(self.runtime, pid=app['pid'], supervisor_pid=supervisor['pid'])
        return {'schema_version': 1, 'kind': 'otelc.rust.execution', 'profile': prepared.PROFILE,
                'request_sha256': prepared.native_launch.digest(original),
                'plan_sha256': prepared.native_launch.digest(resolved), 'supervisor': supervisor,
                'application': app, 'released': True, 'process_exit_code': code,
                'supervisor_exit_code': 128 - code if code < 0 else code, 'exec_verified': False,
                'sdk_health': None, 'inputs_rechecked': True, 'failures': {}, 'source_complete': False}

    def test_protocols_have_exact_reserved_versions(self) -> None:
        expected = {'schema_version': 1, 'kind': 'otelc.rust.protocols', 'profile': prepared.PROFILE,
                    'owned_build': 1, 'source_build': 1, 'source_profile': rust_build.PROFILE,
                    'request': 1, 'plan': 1, 'prepared': 1, 'command': 1, 'execution': 1,
                    'host_release': 1, 'gate': 1, 'native_plan': 2, 'health': 1,
                    'embedded_sdk': True, 'source_complete': False}
        self.assertEqual(prepared.protocols(), expected)
        self.assertEqual(prepared.protocols(source_build_version=2), expected | {
            'source_build': 2, 'source_profile': rust_build.LIFETIME_PROFILE, 'native_plan': 3, 'health': 5})
        for version in (True, 1.0, 0, 3):
            with self.subTest(version=version), self.assertRaises(ValueError):
                prepared.protocols(source_build_version=version)
        self.assertFalse(prepared.same(True, 1))
        self.assertFalse(prepared.same(1, 1.0))

    def test_cli_negotiation_needs_no_configuration_or_tools(self) -> None:
        with patch('sys.argv', ['quux-otelc', '--config', '/missing', 'rust-protocols']), \
                patch.object(cli.Policy, 'load', side_effect=AssertionError('must not read policy')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), prepared.protocols())

    def test_cli_owned_build_and_prepared_run_preserve_explicit_arguments(self) -> None:
        with patch.object(cli.Policy, 'load', return_value=self.policy), \
                patch.object(cli.rust_build, 'read', return_value=self.fixture.source), \
                patch.object(cli.rust_owned_build, 'build', return_value=self.owned) as build, \
                patch('sys.stdout', new_callable=io.StringIO), \
                patch('sys.argv', ['quux-otelc', 'rust-own-build', '--build', '/old', '--output', '/owned']):
            self.assertEqual(cli.main(), 0)
            build.assert_called_once_with(self.policy, self.fixture.source, Path('/owned'))
        with patch.object(cli.Policy, 'load', return_value=self.policy), \
                patch.object(prepared, 'run', return_value=19) as run, \
                patch('sys.argv', ['quux-otelc', 'rust-prepared-run', '--build', '/owned', '--runtime', '/runtime',
                                   '--plain', '--prepare-only', '--', 'literal $arg; *']):
            self.assertEqual(cli.main(), 19)
            run.assert_called_once_with(self.policy, Path('/runtime'), build_directory=Path('/owned'),
                plain=True, prepare_only=True, arguments=('literal $arg; *',))

    def test_prepare_verifies_without_processes_and_command_is_fixed_and_single_use(self) -> None:
        value = self.prepare()
        self.assertEqual(files.read(self.runtime / prepared.PREPARED)[0], value)
        _, plan = self.records()
        self.assertEqual(plan['argv'], [str(self.fixture.source_dir / 'plain'), 'literal $arg; *'])
        self.assertFalse((self.runtime / 'app').exists())
        self.assertEqual(prepared.command(self.runtime), [prepared.sys.executable, '-I', '-S',
            str(prepared.ROOT / 'otelc/rust_supervisor.py'), str(self.runtime)])
        self.starts()
        with self.assertRaisesRegex(ValueError, 'single-use'):
            prepared.command(self.runtime)
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_arguments_and_runtime_aliases_refuse_before_publication(self) -> None:
        for args, lane, budget, identity in ((['bad'], False, 1, 'a'*32), (('a\0',), False, 1, 'a'*32),
                (('x',)*4097, False, 1, 'a'*32), (('x'*1048577,), False, 1, 'a'*32),
                ((), 1, 1, 'a'*32), ((), False, True, 'a'*32), ((), False, 300001, 'a'*32),
                ((), False, 1, 'z'*32)):
            with self.subTest(args=type(args), budget=budget), self.assertRaises(ValueError):
                prepared.request(self.policy, self.runtime, {}, args, lane, budget, identity)
        for directory in (self.fixture.root, self.fixture.source_dir, self.fixture.tools,
                          self.fixture.sdk, self.owned.directory):
            alias = self.base / ('alias-' + directory.name)
            alias.symlink_to(directory, target_is_directory=True)
            for parent in (directory, alias):
                with self.subTest(parent=parent), self.assertRaises(ValueError):
                    prepared.prepare(self.policy, parent / 'runtime', build_directory=self.owned.directory)
                self.assertFalse((parent / 'runtime').exists())

    def test_changed_request_plan_producer_or_build_binding_refuses(self) -> None:
        self.prepare()
        for filename, change in ((prepared.REQUEST, {'schema_version': True}),
                (prepared.REQUEST, {'arguments': None}), (prepared.REQUEST, {'build': {'path': '/no/other.json'}}),
                (prepared.REQUEST, {'build': {'path': 'relative/rust-owned-build.json'}}),
                (prepared.PLAN, {'argv': ['arbitrary-command']}),
                (prepared.PREPARED, {'producer': []}), (prepared.PREPARED, {'interpreter_spelling': 'other'})):
            path = self.runtime / filename
            raw = path.read_bytes()
            try:
                path.write_text(json.dumps(json.loads(raw) | change))
                with self.subTest(filename=filename, change=change), self.assertRaises(ValueError):
                    prepared.verify(self.runtime)
            finally:
                path.write_bytes(raw)

    def test_release_is_exact_and_cannot_be_replayed(self) -> None:
        self.prepare()
        _, app = self.starts()
        for pid, supervisor in ((True, 900002), (900001, True), (900000, 900002), (900001, 900000)):
            with self.subTest(pid=pid, supervisor=supervisor), self.assertRaises(ValueError):
                prepared.release(self.runtime, pid=pid, supervisor_pid=supervisor)
        prepared.release(self.runtime, pid=900001, supervisor_pid=900002)
        with self.assertRaises(FileExistsError):
            prepared.release(self.runtime, pid=900001, supervisor_pid=900002)
        original, plan = self.records()
        for changed in (app | {'pid': True}, app | {'parent_pid': 900000}, app | {'phase': 'running'}):
            with self.assertRaises(ValueError):
                prepared.application(changed, original, plan, 900002)

    def test_execution_binds_signed_exit_and_release(self) -> None:
        self.prepare()
        value = self.final(code=-15)
        path = self.runtime / 'rust-execution.json'
        files.write(path, value)
        self.assertEqual(prepared.execution(self.runtime, supervisor_pid=900002, parent_pid=900003,
                                           returncode=143), value)
        for key, changed in (('exec_verified', True), ('supervisor', {}), ('application', {}),
                ('application', []), ('application', None), ('process_exit_code', 0),
                ('inputs_rechecked', False), ('failures', {'x': 1}), ('schema_version', True),
                ('sdk_health', {}), ('extra', False)):
            path.write_text(json.dumps(value | {key: changed}))
            with self.subTest(key=key), self.assertRaises(ValueError):
                prepared.execution(self.runtime, supervisor_pid=900002, parent_pid=900003, returncode=143)
        path.write_text(json.dumps(value))
        release = self.runtime / 'rust-host-release.json'
        release.write_text(json.dumps(files.read(release)[0] | {'launch_id': 'b'*32}))
        with self.assertRaisesRegex(ValueError, 'host release'):
            prepared.execution(self.runtime, supervisor_pid=900002, parent_pid=900003, returncode=143)

    def test_plain_sdk_files_and_successful_failure_exit_refuse(self) -> None:
        self.prepare()
        value = self.final()
        path = self.runtime / 'rust-execution.json'
        files.write(path, value)
        app = self.runtime / 'app'
        app.mkdir(mode=0o700)
        (app / 'plan.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'plain execution contains'):
            prepared.execution(self.runtime, supervisor_pid=900002, parent_pid=900003, returncode=0)
        (app / 'plan.json').unlink()
        path.write_text(json.dumps(value | {'failures': {'supervisor': 'ValueError'}}))
        with self.assertRaisesRegex(ValueError, 'cannot have a successful'):
            prepared.execution(self.runtime, supervisor_pid=900002, parent_pid=900003, returncode=0)

    def test_waiting_launch_reuses_literal_arguments_and_current_sdk_plan(self) -> None:
        captured = []

        def spawn(gate: native_gate.Product, command: tuple, **options):
            row = {'command': command, 'options': options, 'fds': options.get('pass_fds', ())}
            if row['fds']:
                row['plan'] = json.loads(os.pread(row['fds'][0], 65536, 0))
            captured.append(row)
            return 'waiting'

        with patch.object(native_gate, 'start', side_effect=spawn):
            for plain in (True, False):
                runtime = self.base / ('launch-' + str(plain))
                self.assertEqual(rust_launch.start_waiting(self.fixture.source, self.policy, runtime,
                    self.owned.gate, plain=plain, arguments=('literal $arg; *',), wait_ms=17), 'waiting')
        self.assertEqual(captured[0]['fds'], ())
        self.assertEqual(captured[1]['plan']['language'], 'rust')
        self.assertEqual(captured[1]['plan']['schema_version'], 2)
        self.assertEqual(captured[1]['options']['wait_ms'], 17)
        self.assertEqual(captured[1]['command'][-1], 'literal $arg; *')
        with self.assertRaisesRegex(ValueError, 'wait budget'):
            rust_launch.start_waiting(self.fixture.source, self.policy, self.base / 'bad', self.owned.gate, wait_ms=True)


class RustSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RustPreparedTests('test_protocols_have_exact_reserved_versions')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.runtime
        self.pid = os.getpid()
        self.process = SimpleNamespace(pid=900001, returncode=None)
        self.process.poll = lambda: self.process.returncode

        def wait() -> None:
            self.process.returncode = 0

        def cancel() -> None:
            if self.process.returncode is None:
                self.process.returncode = -15

        self.process.wait = wait
        ready = {'schema_version': 1, 'kind': 'otelc.native.exec-ready', 'pid': 900001,
                 'parent_pid': self.pid, 'process_group': 900001, 'session': 900001}
        self.waiting = SimpleNamespace(process=self.process, ready=Mock(return_value=ready),
                                       release=Mock(), cancel=Mock(side_effect=cancel))
        self.handlers = {}

        def handler(number, action):
            self.handlers[number] = action
            return signal.SIG_DFL

        for patcher in (patch.object(rust_launch, 'start_waiting', return_value=self.waiting),
                patch.object(rust_supervisor.os, 'getpgrp', return_value=self.pid),
                patch.object(rust_supervisor.os, 'getsid', return_value=self.pid),
                patch.object(rust_supervisor.signal, 'signal', side_effect=handler)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def release_record(self, **changes) -> None:
        original, _ = self.fixture.records()
        files.write(self.runtime / 'rust-host-release.json', {'schema_version': 1,
            'kind': 'otelc.rust.host-release', 'launch_id': original['launch_id'],
            'pid': 900001, 'supervisor_pid': self.pid} | changes)

    def test_success_binds_supervisor_child_release_and_cleanup(self) -> None:
        self.fixture.prepare()
        self.release_record()
        self.assertEqual(rust_supervisor.execute(self.runtime), 0)
        self.waiting.release.assert_called_once()
        self.waiting.cancel.assert_called_once()
        report = prepared.execution(self.runtime, supervisor_pid=self.pid, parent_pid=os.getppid(), returncode=0)
        self.assertTrue(report['released'])
        self.assertTrue(report['inputs_rechecked'])
        self.assertIsNone(report['sdk_health'])
        self.assertFalse(report['exec_verified'])
        with self.assertRaises(FileExistsError):
            rust_supervisor.execute(self.runtime)

    def test_stale_release_refuses_and_cancels_the_unreleased_child(self) -> None:
        self.fixture.prepare()
        self.release_record(launch_id='b'*32)
        self.assertEqual(rust_supervisor.execute(self.runtime), 1)
        self.waiting.release.assert_not_called()
        self.waiting.cancel.assert_called_once()
        report = files.read(self.runtime / 'rust-execution.json')[0]
        self.assertEqual(report['failures'], {'supervisor': 'ValueError'})
        self.assertFalse(report['released'])

    def test_missing_release_expires_without_restarting_its_original_budget(self) -> None:
        self.fixture.prepare(wait_ms=1)
        with patch.object(rust_supervisor.time, 'monotonic', side_effect=(0, 1)):
            self.assertEqual(rust_supervisor.execute(self.runtime), 1)
        self.waiting.release.assert_not_called()
        self.waiting.cancel.assert_called_once()

    def test_early_exit_is_a_failure_before_release(self) -> None:
        self.fixture.prepare()
        self.process.returncode = 77
        self.assertEqual(rust_supervisor.execute(self.runtime), 1)
        self.waiting.release.assert_not_called()

    def test_wrong_child_parent_is_refused_before_host_release(self) -> None:
        self.fixture.prepare()
        self.waiting.ready.return_value['parent_pid'] = 900000
        self.assertEqual(rust_supervisor.execute(self.runtime), 1)
        self.waiting.release.assert_not_called()
        self.waiting.cancel.assert_called_once()

    def test_interruption_preserves_signal_exit_and_reaps_owned_child(self) -> None:
        self.fixture.prepare()

        def interrupt():
            self.handlers[signal.SIGTERM](signal.SIGTERM, None)

        self.waiting.ready.side_effect = interrupt
        self.assertEqual(rust_supervisor.execute(self.runtime), 128 + signal.SIGTERM)
        self.waiting.cancel.assert_called_once()
        report = files.read(self.runtime / 'rust-execution.json')[0]
        self.assertEqual(report['failures'], {'supervisor': 'InterruptedError'})

    def test_cleanup_failure_remains_incomplete(self) -> None:
        self.fixture.prepare()
        self.release_record()
        self.waiting.cancel.side_effect = ValueError('literal cleanup failure')
        self.assertEqual(rust_supervisor.execute(self.runtime), 1)
        self.assertEqual(files.read(self.runtime / 'rust-execution.json')[0]['failures'], {'cleanup': 'ValueError'})

    def test_instrumented_health_delegates_to_original_rust_build_policy_and_pid(self) -> None:
        self.fixture.prepare(plain=False)
        self.release_record()
        checkpoint = {'literal': 'SDK shape is tested by the genuine runtime suite'}
        with patch.object(prepared.health, 'read', return_value=checkpoint), \
                patch.object(rust_launch, 'validate_health') as validate:
            self.assertEqual(rust_supervisor.execute(self.runtime), 0)
            prepared.execution(self.runtime, supervisor_pid=self.pid, parent_pid=os.getppid(), returncode=0)
            self.assertEqual(validate.call_count, 2)
            self.assertEqual(validate.call_args.args,
                (checkpoint, self.fixture.fixture.source, self.fixture.policy, 900001))


class RustPreparedHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RustPreparedTests('test_protocols_have_exact_reserved_versions')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.prepare()
        self.fixture.starts()
        self.process = Mock(pid=900002, returncode=0)
        self.process.poll.side_effect = (None, 0)
        self.process.wait.return_value = 0
        self.report = Mock(return_value={})
        for patcher in (patch.object(prepared, 'prepare', return_value={}),
                patch.object(prepared, 'command', return_value=['fixed-supervisor']),
                patch.object(prepared, 'execution', self.report),
                patch.object(prepared.subprocess, 'Popen', return_value=self.process),
                patch.object(prepared.os, 'getpgid', return_value=900001),
                patch.object(prepared.os, 'getsid', return_value=900001),
                patch.object(prepared.subprocess, 'check_output', return_value='900002\n')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_host(self, **options) -> int:
        return prepared.run(self.fixture.policy, self.fixture.runtime,
                            build_directory=self.fixture.owned.directory, plain=True, **options)

    def test_host_checks_actual_os_ownership_then_releases_and_reads_exit(self) -> None:
        self.assertEqual(self.run_host(), 0)
        prepared.subprocess.check_output.assert_called_once_with(
            ['/bin/ps', '-o', 'ppid=', '-p', '900001'], text=True, timeout=3)
        self.assertTrue((self.fixture.runtime / 'rust-host-release.json').is_file())
        self.report.assert_called_once_with(self.fixture.runtime, supervisor_pid=900002,
            parent_pid=os.getpid(), returncode=0)

    def test_prepare_only_starts_no_supervisor(self) -> None:
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(self.run_host(prepare_only=True), 0)
        prepared.subprocess.Popen.assert_not_called()

    def test_early_supervisor_exit_is_read_without_release(self) -> None:
        self.process.poll.side_effect = None
        self.process.poll.return_value = 7
        self.process.returncode = 7
        self.assertEqual(self.run_host(), 7)
        self.assertFalse((self.fixture.runtime / 'rust-host-release.json').exists())

    def test_wrong_kernel_parent_is_refused_and_supervisor_is_stopped(self) -> None:
        self.process.poll.side_effect = None
        self.process.poll.return_value = None
        prepared.subprocess.check_output.return_value = '900000\n'
        with self.assertRaisesRegex(ValueError, 'not owned'):
            self.run_host()
        self.process.terminate.assert_called_once()
        self.assertFalse((self.fixture.runtime / 'rust-host-release.json').exists())

    def test_timeout_reaps_supervisor_without_restarting(self) -> None:
        self.process.poll.side_effect = None
        self.process.poll.return_value = None
        with patch.object(prepared.time, 'monotonic', side_effect=(0, 31)), \
                self.assertRaisesRegex(ValueError, 'did not reach'):
            self.run_host()
        self.process.terminate.assert_called_once()

    def test_interrupted_host_preserves_failure_and_cleans_only_checked_group(self) -> None:
        self.process.poll.side_effect = None
        self.process.poll.return_value = None
        self.process.wait.side_effect = (KeyboardInterrupt(), subprocess.TimeoutExpired('supervisor', 5), 0)
        with patch.object(prepared.os, 'killpg') as kill, self.assertRaises(KeyboardInterrupt):
            self.run_host()
        kill.assert_called_once_with(900001, signal.SIGKILL)
        self.process.kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
