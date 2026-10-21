"""Exercise actual CLI/config/source/compiler/control/SDK witness joins."""
import contextlib
import io
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_python_adapter import Capture, ROOT
from test_python_spans import TraceCapture, trace_plan, receiver, ExportTraceServiceRequest
from test_python_witness_bootstrap import declaration
from quux_otelc_python.witness_wire import decode_record, encode_record
from quux_otelc_python.telemetry import Runtime
import launch


CLI = ROOT / 'target/debug/quux-otelc'
APP = '''import sys
import otelc_witness_provider
def rendezvous():
    print('harness-ready', flush=True)
    if sys.stdin.readline() != 'release\\n':
        raise ValueError('ordinary harness release failed')
def wrapper():
    return otelc_witness_provider.target(41)
def envelope():
    rendezvous()
    return wrapper()
try:
    print(envelope())
except ValueError as error:
    print('caught:', str(error))
'''
PROVIDER = "def target(value):\n    return value + 1\n"


class Stream:
    """Consumer-side framing for tests, independent of producer buffering."""

    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + 5
        while True:
            try:
                self.socket.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    self.socket.close()
                    raise
                time.sleep(0.001)
        self.socket.settimeout(5)
        self.pending = b''

    def receive(self):
        while b'\n' not in self.pending:
            chunk = self.socket.recv(16384)
            if not chunk:
                if self.pending:
                    raise AssertionError('truncated producer frame')
                return None
            self.pending += chunk
        frame, self.pending = self.pending.split(b'\n', 1)
        return decode_record(frame + b'\n')

    def send(self, operation, **fields):
        self.socket.sendall(encode_record({'version': 1, 'kind': 'request', 'op': operation,
                                          'request_id': os.urandom(16).hex(), **fields}))

    def arm(self, ready, manifest, *, stale=False):
        self.send('arm', runtime_id=ready['runtime_id'], context_id=ready['context_id'],
                  ready_seq=ready['ready_seq'] + int(stale), scope_id=os.urandom(16).hex(),
                  build_id=manifest['build_id'], selection_id=manifest['selection_id'],
                  wrapper_probe_id=manifest['roles']['wrapper']['probe_id'],
                  target_probe_id=manifest['roles']['target']['probe_id'])
        return self.receive()


class PythonWitnessLaunchTests(unittest.TestCase):
    def fixture(self, root, endpoint, *, fail=False, noise=False):
        project = root / 'app'; project.mkdir()
        control = root / 'control'; control.mkdir(mode=0o700)
        source = APP
        provider = "def target(value):\n    raise ValueError('original')\n" if fail else PROVIDER
        if noise:
            source = source.replace('import sys\n', 'import sys, threading\n')
            source = source.replace('def rendezvous():', """def background():
    otelc_witness_provider.GO.wait(5)
    otelc_witness_provider.target('noise')
WORKER = threading.Thread(target=background)
WORKER.start()
def rendezvous():""")
            source = source.replace("        raise ValueError('ordinary harness release failed')", """        raise ValueError('ordinary harness release failed')
    otelc_witness_provider.GO.set()
    if not otelc_witness_provider.STARTED.wait(5):
        raise TimeoutError('ordinary background did not enter')""")
            source = source.replace('    return wrapper()', """    value = wrapper()
    otelc_witness_provider.STOP.set()
    WORKER.join(5)
    return value""")
            provider = """import threading
GO, STARTED, STOP = threading.Event(), threading.Event(), threading.Event()
def target(value):
    if value == 'noise':
        STARTED.set()
        if not STOP.wait(5): raise TimeoutError('ordinary background did not stop')
        return 7
    return value + 1
"""
        (project / 'harness.py').write_text(source)
        (project / 'otelc_witness_provider.py').write_text(provider)
        roles, offsets = declaration(project, source)
        selector = root / 'declaration.json'
        selector.write_text(json.dumps({'version': 1, 'primary': 'harness.py', 'roles': roles,
                                        'call_offsets': offsets}))
        config = root / 'policy.toml'
        manifest, sock = root / 'manifest.json', control / 'w.sock'
        config.write_text('schema_version=2\nlanguages=["python"]\n[sources]\ninclude=["*.py"]\n'
                          '[functions]\ninclude=["harness.wrapper","otelc_witness_provider.target"]\n'
                          '[traces]\nenabled=true\nroot_sample_ratio=1.0\n'
                          f'[export]\nendpoint={json.dumps(endpoint)}\ninterval_ms=60000\ntimeout_ms=500\n'
                          '[witness]\nenabled=true\nmode="scoped_action_v1"\n'
                          f'manifest={json.dumps(str(manifest))}\ncontrol_socket={json.dumps(str(sock))}\n')
        return project, config, selector, manifest, sock

    @unittest.skipUnless(CLI.is_file(), 'native CLI required')
    def test_real_cli_binds_normal_exception_unarmed_and_stale_actions(self):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('OTEL_', 'OTELC_'))}
        environment.update(OTELC_PYTHON=sys.executable, OTELC_ADAPTER_ROOT=str(ROOT / 'adapters'))
        for mode in ('return', 'exception', 'unarmed', 'stale', 'noise'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='ow-', dir='/private/tmp') as name, \
                    receiver() as (endpoint, received):
                root = Path(name)
                project, config, selector, manifest, sock = self.fixture(root, endpoint, fail=mode == 'exception', noise=mode == 'noise')
                before = {p.name: p.read_bytes() for p in project.glob('*.py')}
                command = [str(CLI), '--config', str(config), 'python']
                frozen = subprocess.run([*command, '--witness-freeze', str(selector)], cwd=project,
                                        env=environment, capture_output=True, timeout=15)
                self.assertEqual((0, b'', b''), (frozen.returncode, frozen.stdout, frozen.stderr))
                record = json.loads(manifest.read_bytes())
                baseline = subprocess.run([sys.executable, 'harness.py'], input=b'release\n', cwd=project,
                                          env=environment, capture_output=True, timeout=15)
                process = subprocess.Popen([*command, 'harness.py'], cwd=project, env=environment,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           bufsize=0)
                def reap(p=process):
                    if p.poll() is None:
                        p.kill()
                    p.communicate(timeout=5)
                self.addCleanup(reap)
                try:
                    stream = Stream(sock)
                except OSError:
                    if process.poll() is not None:
                        _, diagnostic = process.communicate(timeout=5)
                        self.fail('witness launch failed before control readiness: ' + diagnostic.decode())
                    raise
                self.addCleanup(stream.socket.close)
                ready = stream.receive()
                self.assertEqual('ready', ready['event'])
                ordinary_ready = bytearray()
                while not ordinary_ready.endswith(b'harness-ready\n'):
                    self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                    chunk = process.stdout.read(1)
                    self.assertTrue(chunk, 'application exited before ordinary harness readiness')
                    ordinary_ready.extend(chunk)
                stream.send('capabilities', profile='python-sync-v1')
                capabilities = stream.receive()
                self.assertTrue(capabilities['available'])
                if mode != 'unarmed':
                    arm = stream.arm(ready, record, stale=mode == 'stale')
                    self.assertEqual(mode != 'stale', arm['ok'])
                stdout, stderr = process.communicate(b'release\n', timeout=15)
                self.assertEqual((baseline.returncode, baseline.stdout, baseline.stderr),
                                 (process.returncode, bytes(ordinary_ready) + stdout, stderr))
                events = [ready]
                while True:
                    frame = stream.receive()
                    if frame is None:
                        break
                    events.append(frame)
                self.assertFalse(sock.exists())
                self.assertEqual(before, {p.name: p.read_bytes() for p in project.glob('*.py')})
                self.assertEqual(list(range(1, len(events) + 1)), [e['event_seq'] for e in events])
                self.assertEqual('runtime_end', events[-1]['event'])
                self.assertEqual(mode in ('return', 'exception', 'noise'), events[-1]['complete'])
                spans = [span for path, _, body in received if path == '/v1/traces'
                         for resource in ExportTraceServiceRequest.FromString(body).resource_spans
                         for scope in resource.scope_spans for span in scope.spans]
                self.assertEqual(3 if mode == 'noise' else 2, len(spans))
                if mode in ('return', 'exception', 'noise'):
                    consumed = next(e for e in events if e['event'] == 'consumed')
                    self.assertTrue(events[-1]['health']['export_finished'])
                    self.assertEqual('exception' if mode == 'exception' else 'return', events[-2]['outcome'])
                    for span in spans:
                        attributes = {item.key: item.value.string_value for item in span.attributes}
                        if mode == 'noise' and 'otelc.scope.id' not in attributes:
                            self.assertNotIn('otelc.probe.id', attributes)
                            continue
                        self.assertEqual(consumed['scope_id'], attributes['otelc.scope.id'])
                        self.assertEqual(consumed['root_trace_id'], span.trace_id.hex())
                        role = 'wrapper' if span.name == 'harness.wrapper' else 'target'
                        self.assertEqual(record['roles'][role]['probe_id'], attributes['otelc.probe.id'])
                        self.assertEqual(consumed['root_span_id'], span.span_id.hex() if role == 'wrapper' else span.parent_span_id.hex())

    def test_direct_launcher_measures_original_code_real_socket_and_final_shutdown(self):
        with tempfile.TemporaryDirectory(prefix='ow-api-', dir='/private/tmp') as name:
            root = Path(name)
            project, _, selector, manifest, sock = self.fixture(root, 'http://127.0.0.1:1')
            policy = trace_plan()
            policy['source_matchers']['include'] = [r'(?-u).*\.py$']
            policy['function_matchers']['include'] = [r'(?-u)^(harness\.wrapper|otelc_witness_provider\.target)$']
            policy['witness'] = {'enabled': True, 'mode': 'scoped_action_v1',
                                 'manifest': str(manifest), 'control_socket': str(sock)}
            plan_file = root / 'resolved.json'; plan_file.write_text(json.dumps(policy))
            callbacks, runtimes, failures, events = [], [], [], []
            previous_main = sys.modules['__main__']
            previous_provider = sys.modules.pop('otelc_witness_provider', None)
            previous_argv, previous_path = sys.argv[:], sys.path[:]
            previous_started = launch._APPLICATION_STARTED
            read_fd, write_fd = os.pipe()
            reader, writer = os.fdopen(read_fd), os.fdopen(write_fd, 'w')
            self.addCleanup(reader.close); self.addCleanup(writer.close)

            def runtime(plan, **kwargs):
                instance = Runtime(plan, Capture(), TraceCapture(), **kwargs)
                runtimes.append(instance)
                return instance

            def consumer():
                stream = None
                try:
                    stream = Stream(sock)
                    ready = stream.receive(); events.append(ready)
                    self.assertTrue(stream.arm(ready, json.loads(manifest.read_bytes()))['ok'])
                except BaseException as error:
                    failures.append(error)
                finally:
                    writer.write('release\n'); writer.flush()
                if stream is not None:
                    try:
                        while True:
                            record = stream.receive()
                            if record is None:
                                break
                            events.append(record)
                    except BaseException as error:
                        failures.append(error)
                    finally:
                        stream.socket.close()

            worker = threading.Thread(target=consumer)
            try:
                with contextlib.chdir(project), patch.object(sys, 'stdin', reader), \
                        patch.object(launch, 'Runtime', side_effect=runtime), \
                        patch.object(launch.atexit, 'register', side_effect=lambda function, *args: callbacks.append((function, args))), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(0, launch.main([str(plan_file), '--witness-freeze', str(selector)]))
                    worker.start()
                    self.assertEqual(0, launch.main([str(plan_file), 'harness.py']))
                    function, args = callbacks.pop()
                    function(*args)
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertFalse(failures, failures)
                self.assertEqual('harness-ready\n42\n', output.getvalue())
                self.assertTrue(events[-1]['complete'])
                self.assertEqual(2, len(runtimes[0].traces.delegate.spans()))
            finally:
                for function, args in callbacks:
                    function(*args)
                for instance in runtimes:
                    instance.close()
                if worker.ident is not None:
                    worker.join(5)
                sys.modules['__main__'] = previous_main
                sys.modules.pop('otelc_witness_provider', None)
                if previous_provider is not None:
                    sys.modules['otelc_witness_provider'] = previous_provider
                sys.argv[:], sys.path[:] = previous_argv, previous_path
                launch._APPLICATION_STARTED = previous_started

    def test_frozen_launch_rejects_unknown_declarations_changed_sources_and_other_entry_routes(self):
        from quux_otelc_python.witness_code import CodeBindingError
        from quux_otelc_python.witness_launch import freeze, prepare
        with tempfile.TemporaryDirectory(prefix='ow-reject-', dir='/private/tmp') as name:
            root = Path(name)
            project, _, selector, manifest, sock = self.fixture(root, 'http://127.0.0.1:1')
            policy = trace_plan()
            policy['source_matchers']['include'] = [r'(?-u).*\.py$']
            policy['function_matchers']['include'] = [r'(?-u)^(harness\.wrapper|otelc_witness_provider\.target)$']
            policy['witness'] = {'enabled': True, 'mode': 'scoped_action_v1',
                                 'manifest': str(manifest), 'control_socket': str(sock)}
            plan_file = root / 'resolved.json'; plan_file.write_text(json.dumps(policy))
            valid = selector.read_bytes()
            with contextlib.chdir(project):
                for data in (b'{}', b'{', valid.replace(b'"version": 1', b'"version": 2')):
                    selector.write_bytes(data)
                    with self.assertRaises(CodeBindingError):
                        freeze(policy, str(selector))
                selector.write_bytes(valid)
                freeze(policy, str(selector))
                with self.assertRaises(FileExistsError):
                    freeze(policy, str(selector))
                with self.assertRaisesRegex(CodeBindingError, 'primary_script_path'):
                    prepare(policy, './harness.py')
                with self.assertRaisesRegex(ValueError, 'frozen source script'):
                    launch.main([str(plan_file), '-m', 'harness'])
                with self.assertRaisesRegex(ValueError, 'one declaration'):
                    launch.main([str(plan_file), '--witness-freeze', str(selector), 'extra'])
                (project / 'otelc_witness_provider.py').write_text(PROVIDER + '# changed\n')
                with self.assertRaises(CodeBindingError):
                    prepare(policy, 'harness.py')
