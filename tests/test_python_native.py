"""Native developer sources and real SDK/receiver data through the public CLI."""

from __future__ import annotations

import hashlib
import json
import os
import site
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from otelc import control, python as adapter
from otelc.config import Policy
from otelc.python_sdk_process import SdkProcess
from test_java_integration import Capture, batch

ROOT = Path(__file__).resolve().parents[1]


class PythonNativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.application = self.root / 'application'
        self.application.mkdir()
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.config = self.root / 'otelc.toml'
        self.config.write_text(f'''schema_version = 3
[sources]
include = ["**/*.py"]
[functions]
include = ["*workload*"]
exclude = ["*excluded*"]
[metrics]
collection = false
emission = false
[traces]
enabled = true
sample_ratio = 1.0
[export]
endpoint = "http://127.0.0.1:{self.capture.server.server_port}"
interval_ms = 100000
timeout_ms = 1000
''')

    def launch(self, runtime: Path, *, module: str | None = None,
               arguments: tuple[str, ...] = (), plain: bool = False,
               environment: dict[str, str] | None = None) -> subprocess.Popen[str]:
        command = [sys.executable, '-m', 'otelc', '--config', str(self.config),
            'python', '--root', str(self.application), '--runtime', str(runtime)]
        command += ['--module', module] if module else ['--entry', 'entry.py']
        if plain:
            command.append('--plain')
        command += ['--', *arguments]
        process = subprocess.Popen(command, cwd=ROOT, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()

    def ready(self, process: subprocess.Popen[str], runtime: Path):
        deadline = time.monotonic() + 15
        endpoint = runtime / 'control.json'
        while time.monotonic() < deadline:
            if endpoint.exists():
                return control.request(endpoint, 'status')
            if process.poll() is not None:
                self.fail(f'Python launch exited {process.returncode}: {process.stderr.read()}')
            time.sleep(.01)
        self.fail('Python control endpoint did not become ready')

    def test_original_example_same_pid_four_states_real_spans_and_open_connection_shutdown(self) -> None:
        source = self.application / 'entry.py'
        source.write_bytes((ROOT / 'examples/python/BenchmarkApp.py').read_bytes())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        plain = self.launch(self.root / 'plain', plain=True)
        expected = batch(plain, 3)
        plain.stdin.write('quit\n')
        plain.stdin.flush()
        plain.wait(timeout=10)
        self.assertEqual((plain.returncode, plain.stderr.read()), (0, ''))
        self.assertFalse((self.root / 'plain').exists())
        runtime = self.root / 'runtime'
        process = self.launch(runtime)
        initial = self.ready(process, runtime)
        app_pid = initial['pid']
        self.assertNotEqual(app_pid, process.pid)
        self.assertEqual((initial['collection'], initial['emission']), (False, False))
        for collection, emission in ((False, False), (True, False), (False, True), (True, True)):
            control.request(runtime / 'control.json', 'collection ' + ('on' if collection else 'off'))
            state = control.request(runtime / 'control.json', 'emission ' + ('on' if emission else 'off'))
            self.assertEqual(state['pid'], app_pid)
            self.assertEqual((state['collection'], state['emission']), (collection, emission))
            observed = batch(process, 3)
            self.assertEqual(observed['checksum'], expected['checksum'])
            self.assertEqual(observed['calls'], 3)
            self.assertGreater(observed['elapsed_ns'], 0)
            control.request(runtime / 'control.json', 'flush')
        endpoint = json.loads((runtime / 'control.json').read_text())
        with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=1) as client:
            client.sendall(b'{')
            started = time.monotonic()
            process.stdin.write('quit\n')
            process.stdin.flush()
            process.wait(timeout=6)
            self.assertLess(time.monotonic() - started, 6)
        self.assertEqual((process.returncode, process.stderr.read()), (0, ''))
        report = json.loads((runtime / 'sdk/python-sdk-status.json').read_text())
        self.assertEqual((report['phase'], report['pid']), ('closed', app_pid))
        self.assertNotIn(report['sdk_pid'], (app_pid, process.pid))
        self.assertEqual(report['producer']['function_completions'], 12)
        self.assertEqual(self.capture.calls(), 6)
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans), 12)
        self.assertEqual({span.name for span in spans}, {'entry.workload'})
        pids = {attribute.value.int_value for request in self.capture.traces
                for resource in request.resource_spans for attribute in resource.resource.attributes
                if attribute.key == 'process.pid'}
        self.assertEqual(pids, {app_pid})
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    def test_native_script_module_metadata_original_hook_output_errors_and_exit_codes(self) -> None:
        custom = self.root / 'custom'
        custom.mkdir()
        (custom / 'sitecustomize.py').write_text('import random\nrandom.seed(2468)\nMARKER = "original"\n')
        source = '''import inspect,json,os,random,sys,sitecustomize
def workload(value: int = 3) -> int: return value * 14
print(json.dumps({'result':workload(),'argv':sys.argv[1:],'name':__name__,
 'file':__file__,'cached':__cached__,'package':__package__,
 'spec':None if __spec__ is None else __spec__.name,'loader':type(__loader__).__name__,
 'signature':str(inspect.signature(workload)),'random':[random.random() for _ in range(3)],
 'site_marker':sitecustomize.MARKER,'site_file':sitecustomize.__file__,
 'path':sys.path,'pythonpath':os.environ.get('PYTHONPATH')}))
if '--uncaught' in sys.argv: raise RuntimeError('original failure')
if '--exit' in sys.argv: raise SystemExit(7)
'''
        (self.application / 'entry.py').write_text(source)
        package = self.application / 'package'
        package.mkdir()
        (package / '__init__.py').write_text('')
        (package / '__main__.py').write_text(source)
        environment = {**os.environ, 'PYTHONPATH': str(custom), 'PYTHONDONTWRITEBYTECODE': '1'}
        for index, (module, arguments, expected) in enumerate((
            (None, ('a b', 'α😀'), 0), ('package', ('a b', 'α😀'), 0),
            (None, ('--uncaught',), 1), ('package', ('--uncaught',), 1),
            (None, ('--exit',), 7), ('package', ('--exit',), 7))):
            with self.subTest(module=module, arguments=arguments):
                command = [sys.executable, *(['-m', module] if module else ['entry.py']), *arguments]
                baseline = subprocess.run(command, cwd=self.application, env=environment,
                                          capture_output=True, text=True, timeout=10)
                runtime = self.root / f'runtime-{index}'
                process = self.launch(runtime, module=module, arguments=arguments, environment=environment)
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, expected)
                self.assertEqual((stdout, stderr), (baseline.stdout, baseline.stderr))
                report = json.loads((runtime / 'sdk/python-sdk-status.json').read_text())
                self.assertEqual(report['phase'], 'closed')
                self.assertEqual(report['producer']['function_completions'], 1)

    def test_failed_bootstrap_never_runs_application_code_or_reports_clean(self) -> None:
        marker = self.application / 'app-started'
        entry = self.application / 'entry.py'
        entry.write_text(f'open({str(marker)!r}, "w").close()\n')
        policy = Policy.load(self.config)
        runtime = self.root / 'runtime'
        runtime.mkdir(mode=0o700)
        sdk = SdkProcess.start(policy, runtime / 'sdk', bind=False)
        self.addCleanup(sdk.abort)
        descriptor, environment = adapter.prepare(policy, runtime, sdk, self.application)
        try:
            plan = json.loads((runtime / 'python-plan.json').read_text())
            plan['schema_version'] = 99
            os.ftruncate(descriptor, 0)
            os.write(descriptor, json.dumps(plan).encode())
            os.lseek(descriptor, 0, os.SEEK_SET)
            process = subprocess.Popen([sys.executable, 'entry.py'], cwd=self.application,
                env=environment, pass_fds=(*sdk.channel.descriptors, descriptor),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            sdk.channel.close()
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual((process.returncode, stdout, stderr), (78, '', ''))
            self.assertFalse(marker.exists())
            self.assertEqual(json.loads((runtime / 'python-bootstrap-error.json').read_text())['phase'],
                             'bootstrap_failed')
            report = sdk.wait_report()
            self.assertEqual(report['phase'], 'failed')
            self.assertFalse(report['producer_checkpoint_available'])
        finally:
            os.close(descriptor)

    def test_default_application_sitecustomize_keeps_its_native_presence(self) -> None:
        source = '''import importlib.util,json,sys
print(json.dumps({'loaded':'sitecustomize' in sys.modules,
                 'found':importlib.util.find_spec('sitecustomize') is not None}))
'''
        (self.application / 'entry.py').write_text(source)
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        baseline = subprocess.run([sys.executable, 'entry.py'], cwd=self.application,
                                  env=environment, capture_output=True, text=True, timeout=10)
        process = self.launch(self.root / 'runtime', environment=environment)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual((process.returncode, stdout, stderr),
                         (baseline.returncode, baseline.stdout, baseline.stderr))

    def test_no_original_hook_removes_only_the_temporary_startup_module(self) -> None:
        # Homebrew ships a stdlib sitecustomize. A controlled importer supplies
        # the genuine no-original-hook case, while CPython's actual site.main
        # and import machinery execute the generated hook. -S is a harness
        # detail here, not a supported public CLI instrumentation profile.
        policy = Policy.load(self.config)
        runtime = self.root / 'runtime'
        runtime.mkdir(mode=0o700)
        sdk = SdkProcess.start(policy, runtime / 'sdk', bind=False)
        self.addCleanup(sdk.abort)
        descriptor, environment = adapter.prepare(policy, runtime, sdk, self.application)
        driver = f'''import importlib.abc,importlib.util,json,site,sys
sys.path.extend({site.getsitepackages()!r})
class HookFinder(importlib.abc.MetaPathFinder):
    first = True
    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'sitecustomize': return None
        if self.first:
            self.first = False
            return importlib.util.spec_from_file_location(fullname,{str(runtime / 'bootstrap/sitecustomize.py')!r})
        raise ModuleNotFoundError(name='sitecustomize')
sys.meta_path.insert(0,HookFinder())
site.main()
print(json.dumps({{'loaded':'sitecustomize' in sys.modules}}))
'''
        try:
            process = subprocess.Popen([sys.executable, '-S', '-c', driver], cwd=self.application,
                env=environment, pass_fds=(*sdk.channel.descriptors, descriptor),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            sdk.channel.close()
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual((process.returncode, stderr), (0, ''))
            self.assertEqual(json.loads(stdout), {'loaded': False})
            self.assertEqual(sdk.wait_report(application_pid=process.pid)['phase'], 'closed')
        finally:
            os.close(descriptor)
