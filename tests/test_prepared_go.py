"""Real compiler-free Go launches retain process identity and independent controls."""
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

from ci import build_go_upstream as tooling
from otelc import control, go_build, go_supervisor, prepared_go as prepared
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]
APP = '''package main
import("bufio";_ "embed";"encoding/json";"os";"os/signal";"strconv";"syscall")
//go:embed asset.txt
var asset string
var formatters int
func init(){if os.Getenv("IGNORE_TERM")=="1"{signal.Ignore(syscall.SIGTERM)};if marker:=os.Getenv("APP_MARKER");marker!=""{os.WriteFile(marker,[]byte("started"),0600)}}
type Failure struct{}
func(*Failure) Error()string{formatters++;panic("formatter must not run")}
type WorkItem struct{Input,Output int64}
func(item *WorkItem) Close()error{if item.Input<0{return (*Failure)(nil)};item.Output=0;return nil}
func workload(amount int)(value int,err error){
 item:=&WorkItem{Input:int64(amount)};defer item.Close()
 if amount==99999{panic((*Failure)(nil))}
 if amount<0{return 0,(*Failure)(nil)}
 item.Output=item.Input*2;return int(item.Output),nil
}
func respond(amount int){
 defer func(){if recover()!=nil{json.NewEncoder(os.Stdout).Encode(map[string]any{"panic":true,"formatters":formatters})}}()
 value,err:=workload(amount)
 json.NewEncoder(os.Stdout).Encode(map[string]any{"value":value,"error":err!=nil,"formatters":formatters})
}
func main(){
 json.NewEncoder(os.Stdout).Encode(map[string]any{"arguments":os.Args[1:],"asset":asset})
 scanner:=bufio.NewScanner(os.Stdin)
 for scanner.Scan(){text:=scanner.Text();if text=="quit"{return};if text=="exit7"{os.Exit(7)}
 if text=="log"{os.WriteFile("runtime.log",[]byte("ordinary mutable data"),0600);json.NewEncoder(os.Stdout).Encode(true);continue}
 if text=="mutate"{file,_:=os.OpenFile("main.go",os.O_APPEND|os.O_WRONLY,0600);file.WriteString("\\n//changed\\n");file.Close();json.NewEncoder(os.Stdout).Encode(true);continue}
 value,_:=strconv.Atoi(text);respond(value)
 }
}
'''


class PreparedGoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='prepared-go-', dir=ROOT / 'build')
        cls.base = Path(cls.temporary.name).resolve()
        cls.root = cls.base / 'application'
        cls.root.mkdir()
        (cls.root / 'go.mod').write_text('module example.com/prepared\n\ngo 1.26.0\n')
        (cls.root / 'main.go').write_text(APP)
        (cls.root / 'asset.txt').write_text('frozen asset')
        cls.tools = tooling.build('go', ROOT / 'build/go-upstream')
        cls.policy = Policy.parse({'schema_version': 3, 'languages': ['go'],
            'sources': {'include': ['**/*.go']}, 'functions': {'include': ['*workload']},
            'lifetimes': {'enabled': True, 'include': ['*WorkItem'], 'release_methods': ['Close']},
            'traces': {'enabled': True, 'sample_ratio': 1.0}})
        cls.build = cls.base / 'build'
        go_build.build_project(cls.root, '.', cls.policy, cls.build, cls.tools)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        data = copy.deepcopy(self.policy.data)
        data['export'].update(endpoint=f'http://127.0.0.1:{self.capture.server.server_port}',
                              interval_ms=100000, timeout_ms=200, shutdown_timeout_ms=1000)
        self.current = Policy.parse(data)

    def runtime(self, name: str) -> Path:
        return self.base / (self._testMethodName + '-' + name)

    def start(self, directory: Path, *, policy: Policy | None = None, plain: bool = False,
              arguments: tuple[str, ...] = (), environment: dict | None = None) -> subprocess.Popen:
        with patch('subprocess.Popen', side_effect=AssertionError('prepare must not start any process')):
            prepared.prepare(policy or self.current, directory, build_directory=self.build,
                             arguments=arguments, plain=plain)
            argv = prepared.command(directory)
        process = subprocess.Popen(argv, env=environment, start_new_session=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process: subprocess.Popen) -> None:
        from otelc.prepared_python import stop_group
        stop_group(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def line(self, process: subprocess.Popen) -> object:
        if not select.select([process.stdout], [], [], 10)[0]:
            self.fail('Go application did not produce the next complete line')
        raw = process.stdout.readline()
        self.assertTrue(raw, 'Go application exited before the expected response')
        return json.loads(raw)

    def exchange(self, process: subprocess.Popen, value: str) -> object:
        process.stdin.write((value + '\n').encode())
        process.stdin.flush()
        return self.line(process)

    def finish(self, process: subprocess.Popen, directory: Path, *, exit_code: int = 0) -> dict:
        process.stdin.write(b'quit\n')
        process.stdin.flush()
        self.assertEqual(process.wait(timeout=10), exit_code)
        self.assertEqual(process.stdout.read(), b'')
        self.assertEqual(process.stderr.read(), b'')
        return prepared.execution(directory, supervisor_pid=process.pid, parent_pid=os.getpid(),
                                  returncode=process.returncode)

    def application(self, directory: Path) -> dict:
        deadline = time.monotonic() + 3
        while True:
            try:
                return prepared.read(directory / 'go-application-start.json')
            except FileNotFoundError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.005)

    def test_old_or_mismatched_build_runtime_contract_refuses_before_launch(self) -> None:
        path = self.build / 'build.json'
        original = path.read_bytes()
        try:
            for field, replacement in (('schema_version', 2), ('schema_version', 3),
                    ('runtime_protocols', {'config': 1, 'health': 3}),
                    ('runtime_protocols', {'config': True, 'health': 4})):
                changed = json.loads(original)
                changed[field] = replacement
                path.write_text(json.dumps(changed))
                directory = self.runtime(field + str(replacement))
                with patch('subprocess.Popen', side_effect=AssertionError('no process may start')):
                    with self.assertRaisesRegex(ValueError, 'build schema4'):
                        prepared.prepare(self.current, directory, build_directory=self.build)
                self.assertFalse(directory.exists())
        finally:
            path.write_bytes(original)

    def test_all_startup_states_live_flags_original_errors_panic_and_literal_arguments(self) -> None:
        fake = self.base / 'forbidden-tools'
        fake.mkdir()
        marker = self.base / 'compiler-was-invoked'
        for name in ('go', 'otelc'):
            path = fake / name
            path.write_text('#!/bin/sh\n: > "' + str(marker) + '"\nexit 99\n')
            path.chmod(0o700)
        env = dict(os.environ, PATH=str(fake))
        arguments = ('--option', 'two words', '$(printf bad > ' + str(marker) + ')', "'quoted'", 'line\nbreak', '')
        for collection in (False, True):
            for emission in (False, True):
                data = copy.deepcopy(self.current.data)
                data['metrics'].update(collection=collection, emission=emission)
                service = 'go-prepared-' + str(collection) + str(emission)
                data['resource']['service_name'] = service
                policy = Policy.parse(data)
                directory = self.runtime(str(collection) + str(emission))
                process = self.start(directory, policy=policy, arguments=arguments, environment=env)
                self.assertEqual(self.line(process), {'arguments': list(arguments), 'asset': 'frozen asset'})
                started = self.application(directory)
                self.assertNotEqual(started['pid'], process.pid)
                self.assertEqual(started['supervisor_pid'], process.pid)
                self.assertEqual(started['process_group'], process.pid)
                endpoint = directory / 'control.json'
                status = control.wait_ready(endpoint, started['pid'])
                self.assertEqual((status['collection'], status['emission']), (collection, emission))
                for flags, amount in (((True, True), '21'), ((True, False), '-1'),
                                      ((False, True), '99999'), ((False, False), '21'), ((True, True), '21')):
                    control.request(endpoint, 'collection ' + ('on' if flags[0] else 'off'))
                    state = control.request(endpoint, 'emission ' + ('on' if flags[1] else 'off'))
                    self.assertEqual(state['pid'], started['pid'])
                    before = len(self.capture.metrics)
                    answer = self.exchange(process, amount)
                    if amount == '99999':
                        self.assertEqual(answer, {'panic': True, 'formatters': 0})
                    else:
                        self.assertEqual(answer, {'value': 0 if amount == '-1' else 42,
                                                 'error': amount == '-1', 'formatters': 0})
                    control.request(endpoint, 'flush')
                    self.assertEqual(len(self.capture.metrics), before + int(all(flags)))
                self.assertTrue(self.exchange(process, 'log'))
                final = self.finish(process, directory)
                self.assertEqual(final['failures'], {})
                self.assertTrue(final['inputs_rechecked'])
                self.assertEqual((final['application_pid'], final['sdk_pid']), (started['pid'], started['pid']))
                self.assertEqual(final['sdk_health']['probe_accounting']['function_ended'], '5')
                latest = {}
                for packet in self.capture.metrics:
                    for resource in packet.resource_metrics:
                        attributes = {a.key: a.value.string_value for a in resource.resource.attributes}
                        if attributes['service.name'] == service:
                            for scope in resource.scope_metrics:
                                for metric in scope.metrics:
                                    if metric.name in ('otelc.function.calls', 'otelc.function.errors'):
                                        latest[metric.name] = sum(point.as_int for point in metric.sum.data_points)
                self.assertEqual(latest['otelc.function.calls'], 3)
                self.assertEqual(latest['otelc.function.errors'], 1)
                spans = []
                for packet in self.capture.traces:
                    for resource in packet.resource_spans:
                        attributes = {a.key: a.value.string_value for a in resource.resource.attributes}
                        if attributes['service.name'] == service:
                            spans.extend(span for scope in resource.scope_spans for span in scope.spans)
                functions = [span for span in spans if span.name == 'example.com/prepared.workload']
                self.assertEqual(len(functions), 5)
                self.assertEqual(sum(span.status.code == 2 for span in functions), 2)
                releases = [span for span in spans if any(a.key == 'boundary' and a.value.string_value == 'resource'
                                                        for a in span.attributes)]
                self.assertEqual(len(releases), 4)
                self.assertTrue(all(not span.parent_span_id and len(span.links) == 1 for span in releases))
                self.assertFalse(final['source_complete'])
                with self.assertRaisesRegex(ValueError, 'single-use'):
                    prepared.command(directory)
        self.assertFalse(marker.exists())
        self.assertEqual((self.root / 'runtime.log').read_text(), 'ordinary mutable data')

    def test_plain_and_instrumented_application_channels_match(self) -> None:
        plain_dir, on_dir = self.runtime('plain'), self.runtime('on')
        plain, instrumented = self.start(plain_dir, plain=True), self.start(on_dir)
        self.assertEqual(self.line(plain), self.line(instrumented))
        for value in ('21', '-1', '99999'):
            self.assertEqual(self.exchange(plain, value), self.exchange(instrumented, value))
        baseline, final = self.finish(plain, plain_dir), self.finish(instrumented, on_dir)
        self.assertIsNone(baseline['sdk_pid'])
        self.assertIsNone(baseline['sdk_health'])
        self.assertEqual(final['failures'], {})
        self.assertFalse((plain_dir / 'control.json').exists())

    def test_abrupt_exit_keeps_original_exit_and_missing_sdk_evidence(self) -> None:
        directory = self.runtime('exit')
        process = self.start(directory)
        self.line(process)
        process.stdin.write(b'exit7\n'); process.stdin.flush()
        self.assertEqual(process.wait(timeout=10), 7)
        final = prepared.execution(directory, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=7)
        self.assertEqual(final['application_exit_code'], 7)
        self.assertTrue(final['failures'])
        self.assertIsNone(final['sdk_health'])

    def test_changed_source_and_embedded_inputs_cannot_launch(self) -> None:
        for name in ('main.go', 'asset.txt'):
            path = self.root / name
            original = path.read_bytes()
            directory = self.runtime(name)
            prepared.prepare(self.current, directory, build_directory=self.build)
            try:
                path.write_bytes(original + b'changed')
                with self.assertRaisesRegex(ValueError, 'compiler input changed'):
                    prepared.command(directory)
                self.assertFalse((directory / 'go-supervisor-start.json').exists())
            finally:
                path.write_bytes(original)

    def test_changed_policy_and_injected_command_refuse_before_process_start(self) -> None:
        data = copy.deepcopy(self.current.data)
        data['functions']['include'] = ['*different']
        with self.assertRaisesRegex(ValueError, 'compiled source/context selection'):
            prepared.prepare(Policy.parse(data), self.runtime('bad-policy'), build_directory=self.build)
        directory = self.runtime('bad-argv')
        prepared.prepare(self.current, directory, build_directory=self.build)
        changed = prepared.read(directory / prepared.PLAN)
        changed['argv'] = ['/bin/sh', '-c', 'exit 0']
        (directory / prepared.PLAN).write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'fixed recipe'):
            prepared.command(directory)
        directory = self.runtime('bool-version')
        prepared.prepare(self.current, directory, build_directory=self.build)
        changed = prepared.read(directory / prepared.PLAN)
        changed['schema_version'] = True
        (directory / prepared.PLAN).write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'fixed recipe'):
            prepared.command(directory)

    def test_execution_reader_refuses_changed_actual_owners_sdk_counts_and_exit(self) -> None:
        directory = self.runtime('retained')
        process = self.start(directory)
        self.line(process)
        self.exchange(process, '21')
        record = self.finish(process, directory)
        path = directory / 'go-execution.json'
        original = path.read_bytes()
        for key, value in (('application_pid', process.pid), ('sdk_pid', 99), ('source_complete', True),
                           ('application_exit_code', 7), ('schema_version', True), ('inputs_rechecked', False)):
            changed = copy.deepcopy(record)
            changed[key] = value
            path.write_text(json.dumps(changed))
            with self.subTest(key=key), self.assertRaises(ValueError):
                prepared.execution(directory, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=0)
        path.write_bytes(original)
        for arguments in ({'supervisor_pid': process.pid + 1, 'parent_pid': os.getpid(), 'returncode': 0},
                          {'supervisor_pid': process.pid, 'parent_pid': os.getpid() + 1, 'returncode': 0},
                          {'supervisor_pid': True, 'parent_pid': os.getpid(), 'returncode': 0},
                          {'supervisor_pid': process.pid, 'parent_pid': os.getpid(), 'returncode': 7}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                prepared.execution(directory, **arguments)
        owner_path = directory / 'go-supervisor-start.json'
        owner_original = owner_path.read_bytes()
        changed = json.loads(owner_original); changed['schema_version'] = True
        owner_path.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            prepared.execution(directory, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=0)
        owner_path.write_bytes(owner_original)

    def test_execution_reader_accepts_parent_one_without_allowing_child_one_or_coercion(self) -> None:
        # Model the container PID namespace in completed records. These real
        # processes run on the ordinary host; this is not a PID-1 container test.
        for plain in (True, False):
            directory = self.runtime('parent-one-' + str(plain))
            process = self.start(directory, plain=plain)
            self.line(process)
            record = self.finish(process, directory)
            owner = prepared.read(directory / 'go-supervisor-start.json')
            owner['parent_pid'] = 1
            record['supervisor'] = owner
            (directory / 'go-supervisor-start.json').write_text(json.dumps(owner))
            (directory / 'go-execution.json').write_text(json.dumps(record))
            self.assertEqual(prepared.execution(directory, supervisor_pid=process.pid,
                                               parent_pid=1, returncode=0), record)
            for parent in (0, -1, True, 1.0, process.pid):
                with self.subTest(plain=plain, parent=parent), self.assertRaises(ValueError):
                    prepared.execution(directory, supervisor_pid=process.pid, parent_pid=parent, returncode=0)
            for supervisor in (0, 1, True, 1.0):
                with self.subTest(plain=plain, supervisor=supervisor), self.assertRaises(ValueError):
                    prepared.execution(directory, supervisor_pid=supervisor, parent_pid=1, returncode=0)

    def test_signal_forwarding_keeps_actual_application_signal_and_incomplete_health(self) -> None:
        directory = self.runtime('signal')
        process = self.start(directory)
        self.line(process)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 143)
        result = prepared.execution(directory, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=143)
        self.assertEqual(result['application_exit_code'], -signal.SIGTERM)
        self.assertEqual(result['interrupted_by'], signal.SIGTERM)
        self.assertIsNone(result['sdk_health'])

    def test_public_supervisor_component_coverage_uses_actual_owned_processes(self) -> None:
        # The shipping -I/-S command has no coverage hook. This separate harness
        # traces the unchanged owner source in an owned session, and must not be
        # presented as evidence for the fixed command or used for timings.
        import coverage
        owner_source = Path(go_supervisor.__file__).resolve()
        harness = self.base / 'owner-coverage.py'
        SDK = str(Path(coverage.__file__).resolve().parent.parent)
        harness.write_text('import sys,runpy,subprocess,json\n'
            + 'sys.path[:0]=' + repr([str(ROOT), SDK]) + '\nimport coverage\n'
            + 'from pathlib import Path\n'
            + 'runtime=Path(sys.argv[-1]); expected=json.loads((runtime/"go-plan.json").read_text())["argv"][0]\n'
            + 'spawn=subprocess.Popen\n'
            + 'def only_application(argv,*args,**kwargs):\n'
            + ' if argv[0]!=expected: raise RuntimeError("unexpected subprocess during compiler-free launch")\n'
            + ' return spawn(argv,*args,**kwargs)\nsubprocess.Popen=only_application\n'
            + 'if runtime.name.endswith("coverage-publication"):\n'
            + ' import errno,os,time\n from otelc import python_launch_files as files\n write=files.write\n'
            + ' def broken_write(path,value):\n'
            + '  if path.name=="go-application-start.json":\n'
            + '   deadline=time.monotonic()+5\n'
            + '   while not Path(os.environ["APP_MARKER"]).exists():\n'
            + '    if time.monotonic()>deadline: raise RuntimeError("application did not initialise signal handler")\n'
            + '    time.sleep(.005)\n'
            + '   raise OSError(errno.ENOSPC,"simulated full metadata filesystem")\n'
            + '  return write(path,value)\n files.write=broken_write\n'
            + 'meter=coverage.Coverage(data_file=sys.argv.pop(1),include=['
            + repr(str(owner_source)) + '])\nmeter.start()\n'
            + 'try: runpy.run_path(' + repr(str(owner_source)) + ',run_name="__main__")\n'
            + 'finally: meter.stop();meter.save()\n')
        for name, plain, body, expected in (('stock', True, b'quit\n', 0),
                                             ('sdk', False, b'21\nquit\n', 0),
                                             ('exit', False, b'exit7\n', 7),
                                             ('publication', False, None, 137)):
            directory = self.runtime('coverage-' + name)
            prepared.prepare(self.current, directory, build_directory=self.build, plain=plain)
            data = self.base / ('coverage-' + name)
            environment = dict(os.environ)
            if name == 'publication':
                environment.update(APP_MARKER=str(self.base / 'publication-ready'), IGNORE_TERM='1')
            process = subprocess.Popen([sys.executable, '-I', '-S', '-B', str(harness), str(data),
                                        '--runtime', str(directory)], start_new_session=True, env=environment,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(self.stop, process)
            if body is None:
                process.wait(timeout=10)
                stdout, stderr = process.stdout.read(), process.stderr.read()
            else:
                stdout, stderr = process.communicate(body, timeout=10)
            self.assertEqual(process.returncode, expected, stderr)
            self.assertEqual(stderr, b'')
            self.assertTrue(stdout)
            if name == 'publication':
                result = prepared.read(directory / 'go-execution.json')
                self.assertEqual(result['application_exit_code'], -signal.SIGKILL)
                self.assertFalse(result['inputs_rechecked'])
                self.assertIsNone(result['sdk_health'])
                self.assertIn('simulated full metadata filesystem', result['failures']['execution'])
                with self.assertRaises(ProcessLookupError):
                    os.kill(result['application_pid'], 0)
            else:
                result = prepared.execution(directory, supervisor_pid=process.pid, parent_pid=os.getpid(),
                                            returncode=process.returncode)
                self.assertEqual(bool(result['failures']), name == 'exit')
            current = coverage.Coverage.current()
            if current is not None:
                observed = coverage.CoverageData(basename=str(data))
                observed.read()
                current.get_data().update(observed)

    def test_bad_metadata_never_executes_application_initialisers(self) -> None:
        directory = self.runtime('bad-initialisation')
        command = prepared.prepare(self.current, directory, build_directory=self.build)['command']
        request = directory / prepared.REQUEST
        value = prepared.read(request); value['schema_version'] = True
        request.write_text(json.dumps(value))
        marker = self.base / 'unwanted-app-init'
        process = subprocess.run(command, env=dict(os.environ, APP_MARKER=str(marker)),
                                 start_new_session=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, b'')
        self.assertIn(b'Go prepared launch failed', process.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((directory / 'go-application-start.json').exists())

    def test_unowned_session_and_occupied_runtime_never_start_application_initialisers(self) -> None:
        directory = self.runtime('unowned-session')
        command = prepared.prepare(self.current, directory, build_directory=self.build)['command']
        marker = self.base / 'unowned-app-init'
        environment = dict(os.environ, APP_MARKER=str(marker))
        result = subprocess.run(command, env=environment, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'caller-owned new session', result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((directory / 'go-supervisor-start.json').exists())
        (directory / 'health.json').write_bytes(b'occupied original')
        result = subprocess.run(command, env=environment, capture_output=True,
                                start_new_session=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'single-use', result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual((directory / 'health.json').read_bytes(), b'occupied original')

    def test_sdk_export_failure_is_separate_from_the_original_application_exit(self) -> None:
        self.capture.trace_response_code = 503
        directory = self.runtime('failed-export')
        process = self.start(directory)
        self.line(process)
        self.assertEqual(self.exchange(process, '21'), {'value': 42, 'error': False, 'formatters': 0})
        result = self.finish(process, directory, exit_code=1)
        self.assertEqual(result['application_exit_code'], 0)
        self.assertTrue(result['failures'])
        self.assertGreater(int(result['sdk_health']['trace_exports']['failed']), 0)

    def test_binary_config_changes_arguments_and_ambient_sdk_settings_refuse(self) -> None:
        with patch.dict(os.environ, {'OTEL_SERVICE_NAME': 'uncontrolled'}), self.assertRaisesRegex(ValueError, 'ambient'):
            prepared.prepare(self.current, self.runtime('ambient'), build_directory=self.build)
        for arguments in (['literal'], ('bad\0argument',), (1,), ('a' * (1024 * 1024 + 1),)):
            with self.subTest(arguments_type=type(arguments).__name__), self.assertRaisesRegex(ValueError, 'arguments'):
                prepared.prepare(self.current, self.runtime('bad-args'), build_directory=self.build,
                                 arguments=arguments)
        self.assertFalse(self.runtime('bad-args').exists())
        with self.assertRaisesRegex(ValueError, 'outside checked'):
            prepared.prepare(self.current, self.root / 'runtime', build_directory=self.build)
        directory = self.runtime('changed-binary')
        prepared.prepare(self.current, directory, build_directory=self.build)
        binary = self.build / 'instrumented'
        original = binary.read_bytes()
        try:
            binary.write_bytes(original + b'changed')
            with self.assertRaisesRegex(ValueError, 'binary changed'):
                prepared.command(directory)
        finally:
            binary.write_bytes(original)
        config = directory / 'config.json'
        changed = prepared.read(config)
        changed['collection'] = not changed['collection']
        config.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'runtime configuration'):
            prepared.command(directory)

    def test_actual_cli_protocol_discovery_and_prepare_do_not_require_a_compiler(self) -> None:
        fake = self.base / 'cli-forbidden-tools'
        fake.mkdir()
        marker = fake / 'invoked'
        for name in ('go', 'otelc'):
            path = fake / name
            path.write_text('#!/bin/sh\n: > "' + str(marker) + '"\nexit 99\n')
            path.chmod(0o700)
        environment = dict(os.environ, PATH=str(fake), OTELC_PYTHON=sys.executable)
        cli = ROOT / 'bin/quux-otelc'
        discovered = subprocess.run([str(cli), 'go-protocols'], cwd=self.base, env=environment,
                                    capture_output=True, check=True, timeout=10)
        self.assertEqual(json.loads(discovered.stdout), prepared.protocols())
        config = self.base / 'cli-policy.toml'
        config.write_text('schema_version=3\nlanguages=["go"]\n[sources]\ninclude=["**/*.go"]\n'
            '[functions]\ninclude=["*workload"]\n[lifetimes]\nenabled=true\ninclude=["*WorkItem"]\nrelease_methods=["Close"]\n')
        directory = self.runtime('cli')
        result = subprocess.run([str(cli), '--config', str(config), 'go', '--build', str(self.build),
            '--runtime', str(directory), '--prepare-only', '--', '--literal', 'two words'],
            cwd=self.base, env=environment, capture_output=True, check=True, timeout=10)
        record = json.loads(result.stdout)
        self.assertEqual(record['plan']['argv'][-2:], ['--literal', 'two words'])
        self.assertFalse((directory / 'go-application-start.json').exists())
        self.assertFalse(marker.exists())

    def test_common_example_uses_one_policy_and_preserves_known_checksums_and_lifecycle_results(self) -> None:
        source = ROOT / 'examples/go'
        original = go_build.inputs(source)
        common = copy.deepcopy(Policy.load(ROOT / 'examples/otelc.toml').data)
        common['export'].update(endpoint=f'http://127.0.0.1:{self.capture.server.server_port}',
                                 timeout_ms=200, shutdown_timeout_ms=1000, interval_ms=100000)
        common['traces']['sample_ratio'] = 1.0
        policy = Policy.parse(common)
        build = self.runtime('example-build')
        result = subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', str(ROOT / 'examples/otelc.toml'),
            'go-build', '--root', str(source), '--output', str(build), '--go', self.tools['go']],
            env=dict(os.environ, OTELC_PYTHON=sys.executable), capture_output=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        announced = json.loads(result.stdout)
        self.assertEqual(announced['schema_version'], 3)
        self.assertEqual(announced['allocations'], 2)
        self.assertEqual(announced['release_hooks'], 2)
        receipt = json.loads((build / 'build.json').read_text())
        self.assertEqual([row['symbol'] for row in receipt['selection']['functions']], ['workload'])
        self.assertEqual({row['symbol'] for row in receipt['selection']['types']}, {'WorkItem', 'LifecycleWorkItem'})
        answers = []
        for name, plain in (('plain', True), ('instrumented', False)):
            directory = self.runtime('example-' + name)
            prepared.prepare(policy, directory, build_directory=build, plain=plain)
            process = subprocess.Popen(prepared.command(directory), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            self.addCleanup(self.stop, process)
            stdout, stderr = process.communicate(b'10\nthrow\nlifecycle\ngc\nquit\n', timeout=15)
            self.assertEqual((process.returncode, stderr), (0, b''))
            rows = [json.loads(line) for line in stdout.splitlines()]
            self.assertEqual(rows[0]['checksum'], 41284864)
            self.assertEqual(rows[0]['calls'], 10)
            self.assertGreater(rows[0].pop('elapsed_ns'), 0)
            self.assertEqual(rows[1:], [{'exception': 'error', 'message': 'negative seed'},
                {'construction_failed': True, 'close_failed': True}, {'gc_requested': True}])
            answers.append(rows)
            result = prepared.execution(directory, supervisor_pid=process.pid, parent_pid=os.getpid(),
                                        returncode=process.returncode)
            self.assertEqual(result['failures'], {})
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(go_build.inputs(source), original)


if __name__ == '__main__':
    unittest.main()
