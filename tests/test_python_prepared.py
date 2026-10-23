"""The fixed supervisor preserves native channels and binds actual final owners."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

from otelc.config import Policy
from otelc import prepared_python as prepared
from otelc import python_launch_files as files
from otelc.python_supervisor import Owner
from test_java_integration import Capture


class PreparedPythonTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / 'app'
        self.app.mkdir()
        self.entry = self.app / 'entry.py'
        self.entry.write_text('def workload(): return 42\nassert workload()==42\n')
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.config = self.root / 'otelc.toml'
        self.config.write_text(f'''schema_version=3
languages=["python"]
[sources]
include=["**/*.py"]
[functions]
include=["entry.workload"]
[metrics]
collection=false
emission=false
[traces]
enabled=true
sample_ratio=1.0
[export]
endpoint="http://127.0.0.1:{self.capture.server.server_port}"
interval_ms=100000
timeout_ms=1000
''')
        self.policy = Policy.load(self.config)
        self.counter = 0

    def prepare(self, *, plain: bool = False, module: str | None = None) -> tuple[Path, dict]:
        self.counter += 1
        directory = self.root / f'runtime-{self.counter}'
        value = prepared.prepare(self.policy, directory, root=self.app,
            entry='./entry.py' if module is None else None, module=module,
            arguments=('', 'caf\u00e9'), plain=plain)
        return directory, value

    def test_prepare_has_no_application_or_sdk_side_effects_and_is_reproducible(self) -> None:
        marker = self.app / 'executed'
        self.entry.write_text('from pathlib import Path\nPath("executed").touch()\n')
        directory, value = self.prepare()
        self.assertFalse(marker.exists())
        self.assertFalse((directory / 'sdk').exists())
        self.assertFalse((directory / 'python-inputs.journal').exists())
        self.assertEqual(value['command'], prepared.invocation(directory))
        self.assertEqual(prepared.verify(directory),
                         (value['request'], value['plan'], value['prepared']))
        self.assertEqual(value['request']['paths']['entry']['requested'], './entry.py')
        self.assertFalse(value['source_complete'])
        with self.assertRaises(FileExistsError):
            prepared.prepare(self.policy, directory, root=self.app, entry=self.entry)
        self.assertFalse(marker.exists())

    def test_actual_supervisor_script_module_errors_and_raw_channels_match_native(self) -> None:
        sources = [
            'import json,sys\nsys.stdout.buffer.write(bytes([0,255,10]));sys.stderr.buffer.write(bytes([1,255,10]))\n'
            'print(json.dumps([sys.argv,sys.orig_argv,__file__,__name__,sys._getframe().f_back is None]))\n',
            'raise ValueError("original failure")\n',
            'raise SystemExit(7)\n',
        ]
        for source in sources:
            for module in (None, 'entry'):
                for plain in (False, True):
                    with self.subTest(module=module, plain=plain, source=source[:20]):
                        self.entry.write_text(source)
                        directory, value = self.prepare(plain=plain, module=module)
                        native = subprocess.run([sys.executable, *value['plan']['native_arguments']],
                            cwd=self.app, capture_output=True, timeout=10)
                        result = subprocess.run(value['command'], capture_output=True, timeout=20)
                        self.assertEqual((result.returncode, result.stdout, result.stderr),
                                         (native.returncode, native.stdout, native.stderr))
                        receipt, _ = files.read(directory / 'python-execution.json')
                        self.assertEqual(receipt['phase'], 'closed', receipt)
                        self.assertEqual(receipt['application']['returncode'], native.returncode)
                        self.assertNotEqual(receipt['application']['pid'], receipt['supervisor_pid'])
                        self.assertTrue(receipt['input_recheck'])
                        self.assertFalse(receipt['source_complete'])
                        self.assertEqual(receipt['entry_evidence']['phase'],
                                         'complete' if module is None else 'unavailable')
                        if plain:
                            self.assertIsNone(receipt['sdk'])
                            self.assertIsNone(receipt['bootstrap_recheck'])
                        else:
                            self.assertEqual(receipt['sdk']['phase'], 'closed')
                            self.assertEqual(receipt['sdk']['returncode'], 0)
                            self.assertTrue(receipt['bootstrap_recheck'])
                            self.assertNotIn(receipt['sdk']['pid'],
                                             (receipt['application']['pid'], receipt['supervisor_pid']))
                        self.assertEqual(self.entry.read_text(), source)

    def test_direct_owner_drain_keeps_real_pids_and_source_untouched(self) -> None:
        directory, _ = self.prepare()
        owner = Owner(directory)
        self.assertEqual(owner.execute(), 0)
        receipt, _ = files.read(directory / 'python-execution.json')
        self.assertEqual(receipt['supervisor_pid'], os.getpid())
        self.assertEqual(receipt['application']['pid'], owner.application.pid)
        self.assertEqual(receipt['sdk']['pid'], owner.sdk.process.pid)
        self.assertEqual((owner.application.poll(), owner.sdk.process.poll()), (0, 0))
        self.assertEqual(receipt['failures'], {})

    def test_altered_prepared_request_plan_or_product_is_refused_before_app_start(self) -> None:
        marker = self.app / 'executed'
        self.entry.write_text('from pathlib import Path\nPath("executed").touch()\n')
        for name, change in ((prepared.REQUEST, lambda data: data.update(arguments=['changed'])),
                             (prepared.PLAN, lambda data: data.update(source_complete=True)),
                             (prepared.PREPARED, lambda data: data['driver'].update(sha256='0' * 64))):
            directory, value = self.prepare()
            data, _ = files.read(directory / name)
            change(data)
            (directory / name).write_text(json.dumps(data))
            result = subprocess.run(value['command'], capture_output=True, timeout=20)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (78, b'', b''))
            self.assertFalse(marker.exists())
            self.assertFalse((directory / 'sdk').exists())
            self.assertFalse((directory / 'python-application-start.json').exists())
            self.assertTrue((directory / 'python-supervisor-error.json').is_file())

    def test_changed_policy_after_app_start_is_not_a_clean_final_receipt(self) -> None:
        self.entry.write_text('from pathlib import Path\nPath(' + repr(str(self.config))
                              + ').write_text("changed")\n')
        directory, _ = self.prepare()
        owner = Owner(directory)
        self.assertEqual(owner.execute(), 1)
        receipt, _ = files.read(directory / 'python-execution.json')
        self.assertEqual(receipt['application']['returncode'], 0)
        self.assertEqual(receipt['sdk']['phase'], 'closed')
        self.assertFalse(receipt['input_recheck'])
        self.assertEqual(receipt['failures']['inputs'], 'changed_or_unavailable')

    def test_entry_self_change_runs_original_bytes_but_fails_stable_input_profile(self) -> None:
        self.entry.write_text('from pathlib import Path\nPath(__file__).write_text("changed")\n')
        directory, _ = self.prepare(plain=True)
        owner = Owner(directory)
        self.assertEqual(owner.execute(), 1)
        receipt, _ = files.read(directory / 'python-execution.json')
        self.assertEqual(receipt['application']['returncode'], 0)
        self.assertEqual(receipt['entry_evidence']['phase'], 'complete')
        self.assertFalse(receipt['input_recheck'])

    def test_missing_checkpoint_and_native_finalization_stay_failed_with_actual_exit(self) -> None:
        for code in (0, 9):
            self.entry.write_text(f'import os\nos._exit({code})\n')
            directory, _ = self.prepare()
            owner = Owner(directory)
            self.assertEqual(owner.execute(), code or 1)
            receipt, _ = files.read(directory / 'python-execution.json')
            self.assertEqual(receipt['application']['returncode'], code)
            self.assertEqual(receipt['phase'], 'failed')
            self.assertEqual(receipt['entry_evidence']['phase'], 'failed')
            self.assertIn('sdk', receipt['failures'])

    def test_bootstrap_change_after_app_start_is_reported_after_real_sdk_drain(self) -> None:
        directory = self.root / 'runtime-1'
        target = directory / 'bootstrap/sitecustomize.py'
        self.entry.write_text('from pathlib import Path\nPath(' + repr(str(target))
                              + ').write_text("changed")\n')
        actual, _ = self.prepare()
        self.assertEqual(actual, directory)
        owner = Owner(directory)
        self.assertEqual(owner.execute(), 1)
        receipt, _ = files.read(directory / 'python-execution.json')
        self.assertEqual(receipt['sdk']['phase'], 'closed')
        self.assertFalse(receipt['bootstrap_recheck'])
        self.assertEqual(receipt['failures']['bootstrap'], 'changed_or_unavailable')

    def test_occupied_bootstrap_never_runs_app_and_reaps_already_ready_sdk(self) -> None:
        marker = self.app / 'executed'
        self.entry.write_text('from pathlib import Path\nPath("executed").touch()\n')
        directory, _ = self.prepare()
        occupied = directory / 'bootstrap'
        occupied.write_text('preserve existing data')
        owner = Owner(directory)
        self.assertEqual(owner.execute(), 1)
        self.assertIsNone(owner.application)
        self.assertIsNotNone(owner.sdk.process.poll())
        self.assertFalse(marker.exists())
        self.assertEqual(occupied.read_text(), 'preserve existing data')
        receipt, _ = files.read(directory / 'python-execution.json')
        self.assertIsNone(receipt['application'])
        self.assertIsNone(receipt['sdk']['phase'])

    def test_actual_sdk_death_cannot_be_reported_as_successful_drain(self) -> None:
        directory = self.root / 'runtime-1'
        ready = directory / 'sdk/python-sdk-ready.json'
        self.entry.write_text('import json,os,signal\nfrom pathlib import Path\n'
            'os.kill(json.loads(Path(' + repr(str(ready)) + ').read_text())["pid"],signal.SIGKILL)\n')
        self.prepare()
        owner = Owner(directory)
        self.assertEqual(owner.execute(), 1)
        receipt, _ = files.read(directory / 'python-execution.json')
        self.assertEqual(receipt['application']['returncode'], 0)
        self.assertEqual(receipt['sdk']['returncode'], -signal.SIGKILL)
        self.assertIsNone(receipt['sdk']['phase'])
        self.assertEqual(receipt['failures']['sdk'], 'unverified')

    def test_failed_sdk_readiness_retains_actual_spawn_exit_and_never_starts_app(self) -> None:
        marker = self.app / 'executed'
        self.entry.write_text('from pathlib import Path\nPath("executed").touch()\n')
        directory, _ = self.prepare()
        owner = Owner(directory)
        # The worker is real; only its parent's readiness check is failed.
        with patch('otelc.python_sdk_process.SdkProcess._ready', side_effect=RuntimeError('unready')):
            self.assertEqual(owner.execute(), 1)
        receipt, _ = files.read(directory / 'python-execution.json')
        spawned, _ = files.read(directory / 'sdk/python-sdk-spawned.json')
        self.assertEqual(receipt['sdk']['phase'], 'startup_failed')
        self.assertEqual(receipt['sdk']['pid'], spawned['pid'])
        self.assertIsInstance(receipt['sdk']['returncode'], int)
        self.assertFalse(marker.exists())
        self.assertIsNone(receipt['application'])
        pid = spawned['pid']
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_occupied_final_record_is_preserved_and_makes_completion_fail(self) -> None:
        directory, _ = self.prepare(plain=True)
        path = directory / 'python-execution.json'
        path.write_text('preserve existing data')
        owner = Owner(directory)
        self.assertEqual(owner.execute(), 1)
        self.assertEqual(path.read_text(), 'preserve existing data')
        diagnostic, _ = files.read(directory / 'python-supervisor-error.json')
        self.assertEqual(diagnostic['failures']['final_record'], 'unavailable')

    def test_cli_prepare_only_keeps_original_script_spelling_without_starting_app(self) -> None:
        marker = self.app / 'executed'
        self.entry.write_text('from pathlib import Path\nPath("executed").touch()\n')
        directory = self.root / 'cli-prepared'
        result = subprocess.run([sys.executable, '-m', 'otelc', '--config', str(self.config),
            'python', '--root', str(self.app), '--entry', './entry.py',
            '--runtime', str(directory), '--prepare-only', '--', 'caf\u00e9'],
            capture_output=True, timeout=20)
        self.assertEqual((result.returncode, result.stderr), (0, b''))
        value = json.loads(result.stdout)
        self.assertEqual(value['request']['paths']['entry']['requested'], './entry.py')
        self.assertEqual(value['plan']['native_arguments'], ['./entry.py', 'caf\u00e9'])
        self.assertFalse(marker.exists())
        self.assertFalse((directory / 'sdk').exists())

    def test_supervisor_does_not_run_the_installed_application_site_hook_twice(self) -> None:
        private = self.root / 'private-interpreter'
        venv.EnvBuilder(with_pip=False, symlinks=True).create(private)
        python = private / 'bin/python'
        site = private / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
        site.mkdir(exist_ok=True, parents=True)
        # Homebrew's stdlib sitecustomize can shadow a venv module. A real
        # installed .pth startup hook must run, even in an isolated startup.
        (site / 'application-startup.pth').write_text('import builtins;print("installed application hook")\n')
        self.entry.write_text('print("original")\n')
        directory = self.root / 'private-prepared'
        source = ('import sys,json\nfrom pathlib import Path\nsys.path.insert(0,'
            + repr(str(prepared.ROOT)) + ')\nfrom otelc.config import Policy\n'
            'from otelc import prepared_python as p\nvalue=p.prepare(Policy.parse('
            + repr(self.policy.data) + '),Path(' + repr(str(directory)) + '),root=Path('
            + repr(str(self.app)) + '),entry="entry.py",plain=True)\nprint(json.dumps(value))\n')
        # The metadata host is deliberately no-site. Only the actual native app
        # may run this installed hook; the supervisor owns no app startup work.
        host = subprocess.run([str(python), '-I', '-S', '-c', source],
                              capture_output=True, timeout=20)
        self.assertEqual((host.returncode, host.stderr), (0, b''))
        value = json.loads(host.stdout)
        native = subprocess.run([str(python), 'entry.py'], cwd=self.app,
                                capture_output=True, timeout=10)
        self.assertIn(b'installed application hook\n', native.stdout)
        result = subprocess.run(value['command'], capture_output=True, timeout=20)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (native.returncode, native.stdout, native.stderr))

    def test_public_run_and_command_support_plain_preparation_without_starting_sdk(self) -> None:
        directory = self.root / 'plain-run'
        self.assertEqual(prepared.run(self.policy, directory, root=self.app,
                                      entry=self.entry, plain=True), 0)
        receipt, _ = files.read(directory / 'python-execution.json')
        self.assertIsNone(receipt['sdk'])
        only = self.root / 'only'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(prepared.run(self.policy, only, root=self.app, entry=self.entry,
                                           prepare_only=True), 0)
        self.assertEqual(json.loads(output.getvalue())['command'], prepared.invocation(only))
        self.assertFalse((only / 'sdk').exists())
        command = prepared.command(self.policy, self.root / 'command', root=self.app,
                                   entry=self.entry, plain=True)
        self.assertEqual(command, prepared.invocation(self.root / 'command'))

    def test_term_reaps_the_owned_application_and_sdk(self) -> None:
        self.entry.write_text('import time\ntime.sleep(60)\n')
        directory, value = self.prepare()
        process = subprocess.Popen(value['command'], start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while not (directory / 'control.json').is_file():
                self.assertIsNone(process.poll())
                if time.monotonic() > deadline:
                    self.fail('actual app control did not become ready')
                time.sleep(.01)
            os.kill(process.pid, signal.SIGTERM)
            out, err = process.communicate(timeout=10)
            self.assertEqual((out, err), (b'', b''))
            receipt, _ = files.read(directory / 'python-execution.json')
            for pid in (receipt['application']['pid'], receipt['sdk']['pid']):
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
        finally:
            if process.poll() is None:
                prepared.stop_group(process)
            for stream in (process.stdout, process.stderr):
                stream.close()

    def test_public_owner_cleans_children_when_the_supervisor_is_killed(self) -> None:
        self.entry.write_text('import time\ntime.sleep(60)\n')
        directory = self.root / 'killed-supervisor'
        code = ('from pathlib import Path\nfrom otelc.config import Policy\n'
                'from otelc import prepared_python as p\n'
                'result=p.run(Policy.load(Path(' + repr(str(self.config)) + ')),Path('
                + repr(str(directory)) + '),root=Path(' + repr(str(self.app))
                + '),entry="entry.py")\nprint("owner-result",result)\n')
        process = subprocess.Popen([sys.executable, '-c', code],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        group = None
        try:
            deadline = time.monotonic() + 15
            while not (directory / 'control.json').is_file():
                self.assertIsNone(process.poll())
                if time.monotonic() > deadline:
                    self.fail('app did not become ready before supervisor kill')
                time.sleep(.01)
            started, _ = files.read(directory / 'python-application-start.json')
            group = started['process_group']
            self.assertEqual(group, started['supervisor_pid'])
            os.kill(started['supervisor_pid'], signal.SIGKILL)
            # The app inherits the outer owner's byte pipes. communicate must
            # reach EOF, rather than hang on leaked application/SDK children.
            out, err = process.communicate(timeout=8)
            self.assertEqual((process.returncode, out, err), (0, b'owner-result -9\n', b''))
            for pid in (started['application_pid'], started['sdk_pid']):
                deadline = time.monotonic() + 3
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() > deadline:
                        self.fail('killed supervisor left an owned child alive')
                    time.sleep(.01)
        finally:
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                stream.close()


class PythonLaunchFilesTests(unittest.TestCase):
    def test_private_metadata_rejects_wrong_kinds_modes_and_ambiguous_json(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            root = Path(temporary)
            private = root / 'private'
            private.mkdir(mode=0o700)
            self.assertEqual(files.private_directory(private), private.resolve())
            private.chmod(0o755)
            with self.assertRaises(ValueError):
                files.private_directory(private)
            path = root / 'metadata'
            path.write_text('{"value":1,"value":2}')
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                files.read(path)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                files.read(path)
            path.unlink()
            os.mkfifo(path, 0o600)
            with self.assertRaises(ValueError):
                files.read(path)
            with patch.object(files, 'MAX_METADATA', 2):
                with self.assertRaises(ValueError):
                    files.write(root / 'too-large', {'value': 1})
            self.assertFalse((root / 'too-large').exists())
