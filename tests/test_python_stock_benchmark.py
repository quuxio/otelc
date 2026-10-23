"""Real stock processes have no otelc runtime and retain honest launch evidence."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otelc import benchmark, python_stock_benchmark as stock
from otelc.config import Policy

SOURCE = '''import json,os,sys
for line in sys.stdin:
    if line.strip() == 'quit': break
    print(json.dumps({'pid':os.getpid(), 'cwd':os.getcwd(),
        'executable':sys.executable, 'optimize':sys.flags.optimize,
        'hashseed':os.environ.get('PYTHONHASHSEED'),
        'native':'_otelc_inputs' in sys.modules,
        'otelc':any(name=='otelc' or name.startswith('otelc.') for name in sys.modules),
        'sdk':any(name.startswith('opentelemetry') for name in sys.modules),
        'monitoring':[sys.monitoring.get_tool(index) for index in range(6)]}),flush=True)
'''


class PythonStockBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / 'app'
        (self.app / 'examples/python').mkdir(parents=True)
        (self.app / 'bin').mkdir()
        (self.app / 'otelc').mkdir()
        self.entry = self.app / 'examples/python/BenchmarkApp.py'
        self.entry.write_text(SOURCE)
        for relative in ('bin/quux-otelc', 'requirements.txt', 'requirements-dev.txt',
                         'otelc/python_stock_benchmark.py'):
            (self.app / relative).write_bytes((stock.ROOT / relative).read_bytes())
        patcher = mock.patch.object(stock, 'ROOT', self.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['python']})

    def inspect(self, process):
        process.stdin.write('inspect\n')
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    def test_direct_process_preserves_environment_without_native_observer_bootstrap_or_sdk(self) -> None:
        runtime = self.root / 'runtime'
        with mock.patch.dict(os.environ, PYTHONOPTIMIZE='1', PYTHONHASHSEED='7'), \
             stock.application(self.policy, runtime) as process:
            value = self.inspect(process)
            self.assertEqual(value['pid'], process.pid)
            self.assertEqual(process.pid, process.owner.pid)
            self.assertEqual(os.getpgid(process.pid), process.pid)
            self.assertEqual(value['cwd'], str(self.app))
            self.assertEqual(Path(value['executable']).resolve(), Path(sys.executable).resolve())
            self.assertEqual((value['optimize'], value['hashseed']), (1, '7'))
            self.assertEqual((value['native'], value['otelc'], value['sdk']), (False, False, False))
            self.assertEqual(value['monitoring'], [None] * 6)
            self.assertIsNone(process.supervisor_pid)
            self.assertIsNone(process.sdk_pid)
            self.assertEqual(process.start['command'], [sys.executable, str(self.entry)])
        # Rechecking uses the environment in which the stock lane was launched.
        with mock.patch.dict(os.environ, PYTHONOPTIMIZE='1', PYTHONHASHSEED='7'):
            stock.recheck(process)
        receipt = json.loads((runtime / 'python-execution.json').read_bytes())
        self.assertEqual(receipt['application'], {'pid': process.pid, 'returncode': 0})
        self.assertEqual(receipt['entry_evidence']['phase'], 'unavailable')
        self.assertFalse(receipt['source_complete'])
        for name in ('python-inputs.journal', 'sdk', 'bootstrap'):
            self.assertFalse((runtime / name).exists())
        self.assertEqual(len(process.evidence), 3)

    def test_changed_source_or_policy_cannot_be_a_closed_stock_receipt(self) -> None:
        for fault in ('source', 'policy'):
            with self.subTest(fault=fault):
                self.entry.write_text(SOURCE)
                config = self.root / 'otelc.toml'
                config.write_text('schema_version=3\nlanguages=["python"]\n')
                policy = Policy.load(config)
                runtime = self.root / fault
                with self.assertRaisesRegex(ValueError, 'changed'):
                    with stock.application(policy, runtime) as process:
                        self.inspect(process)
                        target = self.entry if fault == 'source' else config
                        target.write_text(target.read_text() + '\n# changed\n')
                self.assertIsNotNone(process.owner.poll())
                self.assertFalse((runtime / 'python-execution.json').exists())

    def test_start_record_write_failure_still_reaps_the_direct_application(self) -> None:
        started = []
        original_popen, original_write = stock.subprocess.Popen, stock.files.write

        def popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            started.append(process)
            return process

        def write(path, value):
            if path.name == 'python-application-start.json':
                raise OSError('record unavailable')
            return original_write(path, value)

        with mock.patch.object(stock.subprocess, 'Popen', side_effect=popen), \
             mock.patch.object(stock.files, 'write', side_effect=write):
            with self.assertRaisesRegex(OSError, 'record unavailable'):
                with stock.application(self.policy, self.root / 'runtime'):
                    self.fail('failed readiness must not yield')
        # Interpreter metadata probes also use Popen; identify the actual app.
        application = [process for process in started if str(self.entry) in process.args]
        self.assertEqual(len(application), 1)
        self.assertIsNotNone(application[0].poll())

    def test_primary_failure_survives_cleanup_failure_and_group_is_stopped(self) -> None:
        with self.assertRaisesRegex(ValueError, 'original') as raised:
            with mock.patch.object(benchmark, 'stop', side_effect=OSError('stop unavailable')):
                with stock.application(self.policy, self.root / 'runtime') as process:
                    self.inspect(process)
                    original = ValueError('original')
                    raise original
        self.assertIs(raised.exception, original)
        self.assertTrue(any('OSError' in note for note in raised.exception.__notes__),
                        raised.exception.__notes__)
        self.assertIsNotNone(process.owner.poll())

    def test_pipe_cleanup_error_does_not_skip_reaping_the_owned_child(self) -> None:
        with self.assertRaisesRegex(OSError, 'pipe unavailable'):
            with stock.application(self.policy, self.root / 'runtime') as process:
                self.inspect(process)
                original_close = process.owner.stdout.close

                def close():
                    original_close()
                    raise OSError('pipe unavailable')

                # Restore only after the launch context has completed cleanup.
                patcher = mock.patch.object(process.owner.stdout, 'close', side_effect=close)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.assertIsNotNone(process.owner.poll())

    def test_uncooperative_owned_process_is_killed_and_reaped_within_cleanup_bound(self) -> None:
        self.entry.write_text("import json,signal,sys,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nfor line in sys.stdin:\n    print(json.dumps({'ready':True}),flush=True)\nwhile True:time.sleep(.1)\n")
        with mock.patch.object(benchmark, 'stop', side_effect=OSError('cooperative stop failed')):
            with self.assertRaisesRegex(OSError, 'cooperative stop failed'):
                with stock.application(self.policy, self.root / 'runtime') as process:
                    self.assertTrue(self.inspect(process)['ready'])
        self.assertEqual(process.owner.returncode, -signal.SIGKILL)

    def test_existing_output_refuses_before_any_interpreter_is_spawned(self) -> None:
        runtime = self.root / 'existing'
        runtime.mkdir()
        with mock.patch.object(stock.subprocess, 'Popen') as spawn:
            with self.assertRaises(FileExistsError):
                with stock.application(self.policy, runtime):
                    self.fail('existing runtime must refuse')
        spawn.assert_not_called()

    def test_actual_failure_and_bad_or_timed_out_responses_never_publish_closed_evidence(self) -> None:
        for fault in ('exit', 'response', 'timeout'):
            with self.subTest(fault=fault):
                self.entry.write_text("import sys\nsys.stderr.write('original failure\\n')\nraise SystemExit(7)\n"
                                      if fault == 'exit' else SOURCE)
                runtime = self.root / fault
                expected = TimeoutError if fault == 'timeout' else ValueError
                with self.assertRaises(expected):
                    with stock.application(self.policy, runtime) as process:
                        if fault == 'exit':
                            process.owner.wait(timeout=3)
                        elif fault == 'response':
                            benchmark.batch(process, 1)
                        else:
                            with mock.patch('otelc.benchmark.select.select', return_value=([], [], [])):
                                benchmark.batch(process, 1)
                self.assertIsNotNone(process.owner.poll())
                self.assertFalse((runtime / 'python-execution.json').exists())
                if fault == 'exit':
                    self.assertEqual((self.root / 'plain.stderr').read_text(), 'original failure\n')

    @unittest.skipUnless(hasattr(os, 'fork'), 'owned process groups require POSIX')
    def test_killed_application_leader_does_not_leave_its_descendant_running(self) -> None:
        self.entry.write_text('''import json,os,sys,time
child=os.fork()
if child==0:
    time.sleep(30)
    os._exit(0)
for line in sys.stdin:
    if line.strip()=='quit':break
    print(json.dumps({'child':child}),flush=True)
''')
        with self.assertRaisesRegex(ValueError, 'exited unsuccessfully'):
            with stock.application(self.policy, self.root / 'runtime') as process:
                child = self.inspect(process)['child']
                os.kill(process.pid, signal.SIGKILL)
                process.owner.wait(timeout=3)
        self.assertEqual(process.owner.returncode, -signal.SIGKILL)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
