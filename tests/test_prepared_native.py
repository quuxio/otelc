"""Public native formats and real pre-constructor ownership in both lanes."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from otelc import control, native_build, native_gate, native_launch, prepared_native as prepared
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]
APP = r'''#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <unistd.h>
__attribute__((noinline)) int workload(int x){if(x<0)throw std::runtime_error("original");return x+7;}
struct Item{Item(){const char*p=std::getenv("CTOR_MARKER");if(p){FILE*f=std::fopen(p,"w");if(f){std::fputs("ctor\n",f);std::fclose(f);}}workload(1);}};Item item;
int main(int argc,char**argv){std::printf("ready %ld %s\n",(long)getpid(),argc>1?argv[1]:"none");std::fflush(stdout);if(argc>1&&!std::strcmp(argv[1],"oneshot"))return 0;int x;while(std::scanf("%d",&x)==1){if(x==99)return 23;try{std::printf("%d\n",workload(x));}catch(const std::exception&e){std::printf("%s\n",e.what());}std::fflush(stdout);}return 0;}
'''
COVERED = '''import sys
sys.path.insert(0,sys.argv[1])
from otelc.cli_bootstrap import install_sdk_paths
install_sdk_paths()
import coverage
cov=coverage.Coverage(data_file=sys.argv[3],source=[sys.argv[1]+'/otelc'])
cov.start()
from pathlib import Path
import runpy
script=str(Path(sys.argv[1])/'otelc/native_supervisor.py')
runtime=sys.argv[2]
sys.argv=[script,runtime]
try:
    runpy.run_path(script,run_name='__main__')
finally:
    cov.stop();cov.save()
'''


class NativeProtocolTests(unittest.TestCase):
    def test_discovery_does_not_load_configuration_or_execute_a_compiler(self) -> None:
        result = subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', '/no-config', 'native-protocols'],
            env=dict(os.environ, PATH='/no-tools', OTELC_PYTHON=sys.executable), capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), prepared.protocols())
        self.assertEqual(prepared.protocols()['build'], 2)

    def test_wire_values_are_literal_bounded_and_type_exact(self) -> None:
        policy = Policy.parse({'schema_version': 3, 'languages': ['c']})
        for arguments, plain, budget, launch_id in ((['arg'], False, 10, 'a'*32),
                (('bad\0',), False, 10, 'a'*32), ((), 1, 10, 'a'*32), ((), False, True, 'a'*32),
                ((), False, 300001, 'a'*32), ((), False, 1, 'z'*32)):
            with self.subTest(arguments=arguments, plain=plain), self.assertRaises(ValueError):
                prepared.request(policy, Path('/unused'), {}, arguments, plain, budget, launch_id)
        self.assertFalse(prepared.same(True, 1))
        self.assertFalse(prepared.same(1, 1.0))


class PreparedNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='prepared-native-', dir=ROOT / 'build')
        cls.base = Path(cls.temporary.name).resolve()
        cls.root = cls.base / 'application'
        cls.root.mkdir()
        (cls.root / 'main.cpp').write_text(APP)
        cls.original = native_gate.identity(cls.root / 'main.cpp')
        cls.sdk = Path(os.environ['OTELC_NATIVE_SDK_BUILD']) if os.environ.get('OTELC_NATIVE_SDK_BUILD') else cls.base / 'sdk'
        if not os.environ.get('OTELC_NATIVE_SDK_BUILD'):
            native_launch.build_sdk(cls.sdk)
        cls.policy = Policy.parse({'schema_version': 3, 'languages': ['c', 'cpp'],
            'sources': {'include': ['**']}, 'functions': {'include': ['*workload*']},
            'traces': {'enabled': True, 'sample_ratio': 1.0},
            'metrics': {'collection': False, 'emission': False},
            'export': {'interval_ms': 60000, 'timeout_ms': 200, 'shutdown_timeout_ms': 3000}})
        cls.build = cls.base / 'build'
        native_build.build(cls.policy, 'cpp', cls.root, [Path('main.cpp')], cls.build, sdk_directory=cls.sdk)
        cls.coverages = []

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.coverages:
            import coverage
            combined = coverage.Coverage(data_file=str(ROOT / 'build/native-prepared-subprocess.coverage'))
            combined.combine(data_paths=list(map(str, cls.coverages)), strict=True, keep=True)
            combined.save()
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        data = copy.deepcopy(self.policy.data)
        data['export']['endpoint'] = f'http://127.0.0.1:{self.capture.server.server_port}'
        self.current = Policy.parse(data)

    def wait_file(self, path: Path, process: subprocess.Popen) -> dict:
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                self.fail('supervisor ended before publishing ' + path.name + ': ' + str(process.communicate()))
            self.assertLess(time.monotonic(), deadline)
            try:
                return prepared.files.read(path)[0]
            except (FileNotFoundError, prepared.files.PublicationPending):
                pass
            time.sleep(.005)

    def start(self, name: str, *, plain: bool = False, covered: bool = False,
              wait_ms: int = 30000) -> tuple[Path, subprocess.Popen, Path]:
        runtime, marker = self.base / (self._testMethodName + name), self.base / (self._testMethodName + name + '.ctor')
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('prepare cannot start code')):
            prepared.prepare(self.current, runtime, build_directory=self.build,
                             arguments=('literal $value; *',), plain=plain, wait_ms=wait_ms)
            argv = prepared.command(runtime)
        if covered:
            data = self.base / (runtime.name + '.coverage')
            self.coverages.append(data)
            argv = [sys.executable, '-I', '-S', '-c', COVERED, str(ROOT), str(runtime), str(data)]
        process = subprocess.Popen(argv, env=dict(os.environ, PATH='/no-tools', CTOR_MARKER=str(marker)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        self.addCleanup(self.cleanup, runtime, process)
        return runtime, process, marker

    @staticmethod
    def cleanup(runtime: Path, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)

    def line(self, process: subprocess.Popen) -> bytes:
        self.assertTrue(select.select([process.stdout], [], [], 10)[0])
        value = process.stdout.readline()
        self.assertTrue(value)
        return value

    def finish(self, runtime: Path, process: subprocess.Popen) -> dict:
        process.stdin.close()
        process.stdin = None
        self.assertEqual(process.wait(timeout=10), 0, process.stderr.read())
        self.assertEqual(process.stdout.read(), b'')
        self.assertEqual(process.stderr.read(), b'')
        return prepared.execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=0)

    def test_actual_plain_and_woven_wait_before_constructors_and_preserve_channels(self) -> None:
        for plain in (True, False):
            runtime, process, marker = self.start(str(plain), plain=plain, covered=True)
            app = self.wait_file(runtime / 'native-application-start.json', process)
            self.assertEqual(app['parent_pid'], process.pid)
            self.assertEqual(app['process_group'], app['pid'])
            self.assertEqual(os.getpgid(app['pid']), app['pid'])
            self.assertFalse(marker.exists())
            self.assertFalse((runtime / 'app/control.json').exists())
            self.assertFalse(select.select([process.stdout], [], [], 0)[0])
            prepared.release(runtime, pid=app['pid'], supervisor_pid=process.pid)
            self.assertEqual(self.line(process), f'ready {app["pid"]} literal $value; *\n'.encode())
            self.assertEqual(marker.read_text(), 'ctor\n')
            endpoint = runtime / 'app/control.json'
            for index, flags in enumerate(((False, False), (False, True), (True, False), (True, True))):
                if not plain:
                    control.wait_ready(endpoint, app['pid'])
                    control.request(endpoint, 'collection ' + ('on' if flags[0] else 'off'))
                    self.assertEqual(control.request(endpoint, 'emission ' + ('on' if flags[1] else 'off'))['pid'], app['pid'])
                amount = -1 if index == 2 else 10
                process.stdin.write(f'{amount}\n'.encode()); process.stdin.flush()
                self.assertEqual(self.line(process), b'original\n' if amount < 0 else b'17\n')
                if not plain:
                    control.request(endpoint, 'flush')
            final = self.finish(runtime, process)
            self.assertEqual(final['failures'], {})
            self.assertTrue(final['released'] and final['inputs_rechecked'])
            self.assertEqual(final['process_exit_code'], 0)
            self.assertFalse(final['exec_verified'])
            if plain:
                self.assertIsNone(final['sdk_health'])
            else:
                self.assertEqual(final['sdk_health']['pid'], app['pid'])
                self.assertEqual(final['sdk_health']['function_accounting']['ended'], '5')
        self.assertEqual(native_gate.identity(self.root / 'main.cpp'), self.original)

    def test_wrong_release_and_expired_host_never_run_constructors(self) -> None:
        for name, wrong in (('wrong', True), ('timeout', False)):
            runtime, process, marker = self.start(name, plain=True, covered=True, wait_ms=150 if not wrong else 30000)
            app = self.wait_file(runtime / 'native-application-start.json', process)
            if wrong:
                prepared.files.write(runtime / 'native-host-release.json', {'schema_version': 1, 'pid': app['pid'] + 1})
            self.assertEqual(process.wait(timeout=10), 1)
            final = prepared.execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=1)
            self.assertFalse(final['released'])
            self.assertTrue(final['failures'])
            self.assertFalse(marker.exists())

    def test_changed_prepared_plan_or_source_refuses_before_a_process_starts(self) -> None:
        runtime = self.base / 'refused'
        prepared.prepare(self.current, runtime, build_directory=self.build)
        path = runtime / prepared.REQUEST
        raw = path.read_bytes()
        original_request = json.loads(raw)
        for field, invalid in (('schema_version', True), ('build', None), ('arguments', {}), ('policy', [])):
            changed = dict(original_request); changed[field] = invalid
            try:
                path.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    prepared.command(runtime)
            finally:
                path.write_bytes(raw)
        path = runtime / prepared.PLAN
        raw = path.read_bytes()
        value = json.loads(raw); value['argv'][0] = '/metadata/selected/program'
        try:
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'changed'):
                prepared.command(runtime)
        finally:
            path.write_bytes(raw)
        source = self.root / 'main.cpp'; original = source.read_bytes()
        try:
            source.write_bytes(original + b'\n//changed\n')
            with self.assertRaises(ValueError):
                prepared.command(runtime)
        finally:
            source.write_bytes(original)

    def test_execution_refuses_false_identity_completion_and_extra_fields(self) -> None:
        runtime, process, _ = self.start('final', plain=True, covered=True)
        app = self.wait_file(runtime / 'native-application-start.json', process)
        for field in ('pid', 'parent_pid', 'process_group', 'session'):
            path = runtime / 'native-application-start.json'
            raw = path.read_bytes()
            changed = dict(app); changed[field] = float(changed[field])
            try:
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    prepared.release(runtime, pid=app['pid'], supervisor_pid=process.pid)
            finally:
                path.write_bytes(raw)
        prepared.release(runtime, pid=app['pid'], supervisor_pid=process.pid)
        self.line(process)
        final = self.finish(runtime, process)
        with self.assertRaisesRegex(ValueError, 'single-use'):
            prepared.command(runtime)
        path = runtime / 'native-execution.json'; raw = path.read_bytes()
        for field, wrong in (('exec_verified', True), ('source_complete', True),
                             ('schema_version', True), ('released', 1), ('inputs_rechecked', 1),
                             ('process_exit_code', False), ('profile', 'unknown'),
                             ('process_exit_code', 23), ('process_exit_code', -9),
                             ('application', None), ('sdk_health', {}), ('extra', 'field')):
            changed = dict(final); changed[field] = wrong
            try:
                path.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    prepared.execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=0)
            finally:
                path.write_bytes(raw)

    def test_nonzero_application_exit_and_interrupted_wait_have_distinct_results(self) -> None:
        for name, interrupt in (('nonzero', False), ('signal', True)):
            runtime, process, marker = self.start(name, plain=True, covered=True)
            app = self.wait_file(runtime / 'native-application-start.json', process)
            if interrupt:
                process.send_signal(signal.SIGTERM)
            else:
                prepared.release(runtime, pid=app['pid'], supervisor_pid=process.pid)
                self.line(process)
                process.stdin.write(b'99\n'); process.stdin.flush()
            code = 128 + signal.SIGTERM if interrupt else 23
            self.assertEqual(process.wait(timeout=10), code)
            final = prepared.execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=code)
            self.assertEqual(final['released'], not interrupt)
            self.assertEqual(marker.exists(), not interrupt)
            if interrupt:
                self.assertTrue(final['failures'])
            else:
                self.assertEqual(final['process_exit_code'], 23)
                self.assertTrue(final['inputs_rechecked'])
                self.assertEqual(final['failures'], {})

    def test_public_host_runs_both_lanes_and_prepare_only_starts_nothing(self) -> None:
        import contextlib
        import io
        runtime = self.base / 'public-prepare'
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('prepare-only started a child')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(prepared.run(self.current, runtime, build_directory=self.build,
                                         arguments=('oneshot',), prepare_only=True), 0)
        self.assertEqual(json.loads(output.getvalue())['kind'], 'otelc.native.prepared')
        self.assertFalse((runtime / 'native-supervisor-start.json').exists())
        for plain in (True, False):
            runtime = self.base / ('public-run-' + str(plain))
            original_read = prepared.files.read
            pending = []

            def read(path: Path) -> tuple[dict, bytes]:
                if path.name == 'native-application-start.json' and not pending:
                    pending.append(path)
                    raise prepared.files.PublicationPending('publication not complete')
                return original_read(path)

            with patch.dict(os.environ, {'PATH': '/no-tools'}), patch.object(prepared.files, 'read', side_effect=read):
                self.assertEqual(prepared.run(self.current, runtime, build_directory=self.build,
                                             arguments=('oneshot',), plain=plain), 0)
            self.assertEqual(len(pending), 1)
            final = prepared.files.read(runtime / 'native-execution.json')[0]
            self.assertEqual(final['failures'], {})
            self.assertTrue(final['released'] and final['inputs_rechecked'])

    def test_killed_application_preserves_signal_and_missing_sdk_checkpoint(self) -> None:
        for plain in (True, False):
            runtime, process, _ = self.start('killed-' + str(plain), plain=plain, covered=True)
            app = self.wait_file(runtime / 'native-application-start.json', process)
            prepared.release(runtime, pid=app['pid'], supervisor_pid=process.pid)
            self.line(process)
            os.kill(app['pid'], signal.SIGKILL)
            self.assertEqual(process.wait(timeout=10), 128 + signal.SIGKILL)
            final = prepared.execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(),
                                       returncode=process.returncode)
            self.assertEqual(final['process_exit_code'], -signal.SIGKILL)
            self.assertIsNone(final['sdk_health'])
            self.assertEqual(final['inputs_rechecked'], plain)
            self.assertEqual(bool(final['failures']), not plain)

    def test_host_refusal_reaps_the_waiting_child_without_running_its_constructor(self) -> None:
        runtime, marker = self.base / 'host-refusal', self.base / 'host-refusal.ctor'
        with patch.dict(os.environ, {'CTOR_MARKER': str(marker)}), \
                patch.object(prepared, 'release', side_effect=ValueError('host refuses release')), \
                self.assertRaisesRegex(ValueError, 'host refuses release'):
            prepared.run(self.current, runtime, build_directory=self.build, plain=True)
        self.assertFalse(marker.exists())
        app = prepared.files.read(runtime / 'native-application-start.json')[0]
        final = prepared.files.read(runtime / 'native-execution.json')[0]
        self.assertFalse(final['released'])
        self.assertTrue(final['failures'])
        with self.assertRaises(ProcessLookupError):
            os.kill(app['pid'], 0)

    def test_public_c_build_and_both_execution_lanes_preserve_the_result(self) -> None:
        root, build = self.base / 'c-source', self.base / 'c-build'
        root.mkdir()
        source = root / 'main.c'
        source.write_text('__attribute__((noinline)) int workload(int x){return x+7;}\n'
                          'int main(void){return workload(10)==17?0:1;}\n')
        original = source.read_bytes()
        policy = self.base / 'c-policy.toml'
        policy.write_text('schema_version=3\nlanguages=["c","cpp"]\n'
                          '[sources]\ninclude=["**"]\n[functions]\ninclude=["*workload*"]\n'
                          '[metrics]\ncollection=false\nemission=false\n'
                          '[traces]\nenabled=true\nsample_ratio=1.0\n'
                          f'[export]\nendpoint="http://127.0.0.1:{self.capture.server.server_port}"\n'
                          'interval_ms=60000\ntimeout_ms=200\nshutdown_timeout_ms=3000\n')
        cli = [str(ROOT / 'bin/quux-otelc'), '--config', str(policy)]
        environment = dict(os.environ, OTELC_PYTHON=sys.executable)
        compiled = subprocess.run([*cli, 'native-build', '--language', 'c', '--root', str(root),
            '--source', 'main.c', '--output', str(build), '--sdk-build', str(self.sdk)],
            env=environment, capture_output=True, timeout=180)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertEqual(json.loads(compiled.stdout)['schema_version'], 2)
        for plain in (True, False):
            runtime = self.base / ('c-run-' + str(plain))
            completed = subprocess.run([*cli, 'native-run', '--build', str(build), '--runtime', str(runtime),
                                       *(['--plain'] if plain else [])],
                env=environment | {'PATH': '/no-tools'}, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            self.assertEqual((completed.returncode, completed.stdout, completed.stderr), (0, b'', b''))
            final = prepared.files.read(runtime / 'native-execution.json')[0]
            self.assertEqual(final['failures'], {})
            self.assertTrue(final['inputs_rechecked'])
            if not plain:
                self.assertEqual(final['sdk_health']['function_accounting']['ended'], '1')
        self.assertEqual(source.read_bytes(), original)

    def test_execution_health_is_bound_to_the_verified_language_and_selection(self) -> None:
        runtime, process, _ = self.start('health', covered=True)
        app = self.wait_file(runtime / 'native-application-start.json', process)
        prepared.release(runtime, pid=app['pid'], supervisor_pid=process.pid)
        self.line(process)
        final = self.finish(runtime, process)
        execution, checkpoint = runtime / 'native-execution.json', runtime / 'app/health.json'
        execution_raw, checkpoint_raw = execution.read_bytes(), checkpoint.read_bytes()
        for field, altered in (('language', 'java'), ('selection_sha256', '0' * 64)):
            changed = copy.deepcopy(final)
            changed['sdk_health'][field] = altered
            try:
                execution.write_text(json.dumps(changed))
                checkpoint.write_text(json.dumps(changed['sdk_health']))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    prepared.execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=0)
            finally:
                execution.write_bytes(execution_raw)
                checkpoint.write_bytes(checkpoint_raw)

    def test_build_refuses_a_rehashed_decoy_sdk_record_binding(self) -> None:
        metadata = self.build / native_build.MANIFEST
        original = metadata.read_bytes()
        with tempfile.NamedTemporaryFile(prefix='decoy-', dir=self.sdk, delete=False) as stream:
            stream.write(b'This file is not the SDK build record.\n')
            decoy = Path(stream.name)
        self.addCleanup(decoy.unlink)
        changed = json.loads(original)
        changed['sdk'] = native_gate.identity(decoy)
        try:
            metadata.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, 'SDK build binding'):
                native_build.verify(self.build, self.current)
        finally:
            metadata.write_bytes(original)
