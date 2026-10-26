"""Public unchanged-source RNG controls, including real exporter HTTP retries."""
from __future__ import annotations

from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from otelc import control
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = '''import hashlib,json,pickle,random,sys
def workload(): return random.random()
def state(): return hashlib.sha256(pickle.dumps(random.getstate(),protocol=4)).hexdigest()
def run():
 random.seed(12345)
 print(json.dumps({'values':[workload() for _ in range(3)],'state':state()}),flush=True)
 for command in sys.stdin:
  if command == 'quit\\n': break
  if command == 'state\\n': print(json.dumps({'state':state()}),flush=True)
if __name__ == '__main__': run()
'''


class RetryCapture(Capture):
    """Decode genuine bodies while failing the first request of each signal."""

    def __init__(self, fail_first: bool) -> None:
        super().__init__()
        self.responses: dict[str, list[int]] = {}
        self.lock = threading.Lock()
        owner = self
        original = self.server.RequestHandlerClass

        class Handler(original):
            def send_response(self, code: int, message: str | None = None) -> None:
                with owner.lock:
                    statuses = owner.responses.setdefault(self.path, [])
                    if fail_first and code == 200 and not statuses:
                        code = 503
                    statuses.append(code)
                super().send_response(code, message)

        self.server.RequestHandlerClass = Handler

    def statuses(self, path: str) -> tuple[int, ...]:
        with self.lock:
            return tuple(self.responses.get(path, ()))


class RetryCaptureTests(unittest.TestCase):
    def test_first_actual_http_request_fails_then_same_signal_recovers(self) -> None:
        with closing(RetryCapture(True)) as capture:
            for path in ('/v1/traces', '/v1/metrics'):
                for expected in (503, 200):
                    with closing(http.client.HTTPConnection('127.0.0.1', capture.server.server_port, timeout=2)) as client:
                        client.request('POST', path, body=b'')
                        response = client.getresponse()
                        self.assertEqual((response.status, response.read()), (expected, b''))
                self.assertEqual(capture.statuses(path), (503, 200))
            self.assertEqual((len(capture.traces), len(capture.metrics)), (2, 2))

    def test_success_control_never_invents_a_retry(self) -> None:
        with closing(RetryCapture(False)) as capture:
            with closing(http.client.HTTPConnection('127.0.0.1', capture.server.server_port, timeout=2)) as client:
                client.request('POST', '/v1/traces', body=b'')
                response = client.getresponse()
                self.assertEqual((response.status, response.read()), (200, b''))
            self.assertEqual(capture.statuses('/v1/traces'), (200,))


class PythonRandomStateTests(unittest.TestCase):
    def setUp(self) -> None:
        retained = os.environ.get('OTELC_PYTHON_RANDOM_TEST_OUTPUT')
        if retained:
            self.root = Path(retained).resolve() / self._testMethodName
            self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        else:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name).resolve()
        self.application = self.root / 'application'
        self.application.mkdir()
        self.source = self.application / 'entry.py'
        self.source.write_text(APPLICATION)

    def line(self, process: subprocess.Popen[str]) -> str:
        self.assertTrue(select.select([process.stdout], [], [], 20)[0], 'application output deadline')
        value = process.stdout.readline()
        self.assertTrue(value, 'application exited before its result')
        return value

    @staticmethod
    def stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def exercise(self, fail_first: bool) -> None:
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        baseline = subprocess.run([sys.executable, 'entry.py'], cwd=self.application,
            input='state\nquit\n', capture_output=True, text=True, timeout=10)
        self.assertEqual((baseline.returncode, baseline.stderr), (0, ''))
        expected, final = map(json.loads, baseline.stdout.splitlines())
        self.assertEqual(expected['values'], [0.41661987254534116, 0.010169169457068361, 0.8252065092537432])
        self.assertEqual(final['state'], expected['state'])
        (self.root / 'baseline.stdout').write_text(baseline.stdout)
        for metrics, traces in ((False, False), (True, False), (False, True), (True, True)):
            name = f'metrics-{int(metrics)}-traces-{int(traces)}'
            with self.subTest(mode=name), closing(RetryCapture(fail_first)) as capture:
                case = self.root / name
                case.mkdir()
                config = case / 'otelc.toml'
                config.write_text(f'''schema_version=3
languages=["python"]
[sources]
include=["**/*.py"]
[functions]
include=["*workload*"]
[metrics]
collection={str(metrics).lower()}
emission={str(metrics).lower()}
[traces]
enabled={str(traces).lower()}
sample_ratio=1.0
[export]
endpoint="http://127.0.0.1:{capture.server.server_port}"
interval_ms=50
timeout_ms=3000
shutdown_timeout_ms=5000
''')
                config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
                runtime = case / 'runtime'
                command = [sys.executable, '-I', '-S', '-B', str(ROOT / 'otelc/cli_bootstrap.py'),
                    '--config', str(config), 'python', '--root', str(self.application),
                    '--runtime', str(runtime), '--entry', 'entry.py']
                process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.addCleanup(self.stop, process)
                first = self.line(process)
                self.assertEqual(json.loads(first), expected)
                deadline = time.monotonic() + 12
                paths = [path for enabled, path in ((metrics, '/v1/metrics'), (traces, '/v1/traces')) if enabled]
                while any(200 not in capture.statuses(path) for path in paths) and time.monotonic() < deadline:
                    time.sleep(.01)
                if metrics:
                    # Observe a second periodic export, rather than only a final flush.
                    while capture.statuses('/v1/metrics').count(200) < 2 and time.monotonic() < deadline:
                        time.sleep(.01)
                for path in paths:
                    statuses = capture.statuses(path)
                    self.assertIn(200, statuses)
                    self.assertEqual(statuses[0], 503 if fail_first else 200)
                    self.assertEqual(statuses.count(503), int(fail_first))
                if metrics:
                    self.assertGreaterEqual(capture.statuses('/v1/metrics').count(200), 2)
                control.request(runtime / 'control.json', 'flush')
                process.stdin.write('state\nquit\n'); process.stdin.flush()
                rest, stderr = process.communicate(timeout=15)
                stdout = first + rest
                (case / 'result.json').write_text(json.dumps({'source_sha256': original,
                    'source_after_sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                    'config_sha256': config_hash,
                    'config_after_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
                    'stdout': stdout, 'stderr': stderr, 'returncode': process.returncode,
                    'responses': capture.responses, 'python': sys.version, 'command': command,
                    'scope': 'Seeded application state and real exporter success/retry; not whole-program state equivalence'}, indent=2) + '\n')
                self.assertEqual((process.returncode, stdout, stderr), (baseline.returncode, baseline.stdout, baseline.stderr))
                report = json.loads((runtime / 'sdk/python-sdk-status.json').read_text())
                self.assertEqual((report['phase'], report['sdk_phase']), ('closed', 'closed'))
                self.assertEqual(report['producer']['function_completions'], 3)
                self.assertEqual(report['producer']['runtime_losses'], {})
                self.assertEqual(report['span_accounting']['sdk_exported'], 3 if traces else 0)
                self.assertEqual(report['span_accounting']['queue_dropped'], 0)
                self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)
                self.assertEqual(hashlib.sha256(config.read_bytes()).hexdigest(), config_hash)
                for path, enabled in (('/v1/metrics', metrics), ('/v1/traces', traces)):
                    self.assertEqual(bool(capture.statuses(path)), enabled)

    def test_public_cli_seeded_state_with_all_signal_modes_and_successful_exports(self) -> None:
        self.exercise(False)

    def test_public_cli_seeded_state_with_all_signal_modes_and_real_http_retries(self) -> None:
        self.exercise(True)


if __name__ == '__main__':
    unittest.main()
