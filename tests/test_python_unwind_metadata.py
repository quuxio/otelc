"""Keep exception-instance hooks outside cancellation classification."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_python_adapter import Capture, ROOT, plan
from test_python_spans import ExportTraceServiceRequest, receiver
from quux_otelc_python.monitor import Monitor
from quux_otelc_python.telemetry import Runtime


CLI = ROOT / 'target/debug/quux-otelc'
SOURCE = '''import asyncio, json, sys
MODE, KIND = sys.argv[1:]
reads = 0
Base = {'ordinary': Exception, 'cancelled': asyncio.CancelledError, 'generator': GeneratorExit}[KIND]
class OriginalError(Base):
    def __getattribute__(self, name):
        global reads
        if name == '__class__':
            reads += 1
            if MODE == 'raise':
                raise AssertionError('application class getter was invoked')
            if MODE == 'spoof':
                return asyncio.CancelledError
        return super().__getattribute__(name)
ERROR = OriginalError('original')
def target():
    raise ERROR
try:
    target()
except OriginalError as caught:
    print(json.dumps({'same': caught is ERROR, 'reads': reads}))
'''


class PythonUnwindMetadataTests(unittest.TestCase):
    def test_actual_monitor_does_not_read_exception_class_in_metrics_only_mode(self):
        source = '''reads = 0
class OriginalError(Exception):
    def __getattribute__(self, name):
        global reads
        if name == '__class__': reads += 1
        return super().__getattribute__(name)
def target():
    raise OriginalError('original')
def run():
    global reads
    reads = 0
    try:
        target()
    except OriginalError:
        return reads
'''
        namespace = {}
        exec(compile(source, str(ROOT / 'trace_fixture.py'), 'exec'), namespace)
        baseline = namespace['run']()
        policy = plan()
        policy['source_matchers']['include'] = [r'(?-u)^trace_fixture\.py$']
        policy['function_matchers']['include'] = [r'(?-u)^trace_fixture\.target$']
        runtime = Runtime(policy, Capture())
        monitor = Monitor(policy, runtime)
        try:
            monitor.install()
            measured = namespace['run']()
        finally:
            monitor.close()
            runtime.close()
        self.assertEqual(0, baseline)
        self.assertEqual(baseline, measured)

    @unittest.skipUnless(CLI.is_file(), 'make python-check builds the native CLI')
    def test_public_cli_preserves_getters_identity_and_actual_cancellation_metadata(self):
        with tempfile.TemporaryDirectory() as name, receiver() as (endpoint, requests):
            root = Path(name)
            source = root / 'entry.py'; source.write_text(SOURCE)
            config = root / 'policy.toml'
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('OTEL_', 'OTELC_'))}
            cases = [('count', 'ordinary'), ('raise', 'ordinary'), ('spoof', 'ordinary'),
                     ('raise', 'cancelled'), ('raise', 'generator')]
            for metrics, traces in ((True, False), (False, True), (True, True)):
                config.write_text('schema_version=2\nlanguages=["python"]\n[sources]\ninclude=["entry.py"]\n'
                                  '[functions]\ninclude=["entry.target"]\n'
                                  f'[metrics]\nenabled={str(metrics).lower()}\n[traces]\nenabled={str(traces).lower()}\nroot_sample_ratio=1.0\n'
                                  f'[export]\nendpoint={json.dumps(endpoint)}\ntimeout_ms=500\ninterval_ms=60000\n')
                original = config.read_bytes()
                for mode, kind in cases:
                    with self.subTest(metrics=metrics, traces=traces, mode=mode, kind=kind):
                        requests.clear()
                        baseline = subprocess.run([sys.executable, source.name, mode, kind], cwd=root,
                                                  env=environment, capture_output=True, timeout=15)
                        measured = subprocess.run([str(CLI), '--config', str(config), 'python', source.name, mode, kind],
                                                  cwd=root, env=dict(environment, OTELC_PYTHON=sys.executable,
                                                                    OTELC_ADAPTER_ROOT=str(ROOT / 'adapters')),
                                                  capture_output=True, timeout=15)
                        self.assertEqual((baseline.returncode, baseline.stdout, baseline.stderr),
                                         (measured.returncode, measured.stdout, measured.stderr))
                        self.assertEqual({'same': True, 'reads': 0}, json.loads(measured.stdout))
                        spans = [span for path, _, body in requests if path == '/v1/traces'
                                 for resource in ExportTraceServiceRequest.FromString(body).resource_spans
                                 for scope in resource.scope_spans for span in scope.spans]
                        self.assertEqual(1 if traces else 0, len(spans))
                        if spans:
                            self.assertEqual(2, spans[0].status.code)
                            attributes = {item.key: item.value for item in spans[0].attributes}
                            self.assertEqual(kind != 'ordinary',
                                             'otelc.cancelled' in attributes and attributes['otelc.cancelled'].bool_value)
                        self.assertEqual(SOURCE, source.read_text())
                        self.assertEqual(original, config.read_bytes())
