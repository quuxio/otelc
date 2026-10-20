"""Compare real public CLI exception channels against ordinary CPython."""
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest
import zipfile

from test_python_adapter import ROOT, launch
from test_python_spans import receiver
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


CLI = ROOT / 'target/debug/quux-otelc'


ERRORS = {
    'ordinary': 'def fail():\n    raise ValueError("original failure")\nfail()\n',
    'chain': '''def fail():
    try:
        raise KeyError("original cause")
    except KeyError as cause:
        error = ValueError("original failure")
        error.add_note("original note")
        raise error from cause
fail()
''',
    'group': 'def fail():\n    raise ExceptionGroup("original group", [ValueError("one"), TypeError("two")])\nfail()\n',
    'hook': '''import json, sys
EXPECTED = ValueError("kept")
def hook(kind, value, tb):
    frames = []
    cursor = tb
    while cursor is not None:
        frames.append([cursor.tb_frame.f_code.co_filename, cursor.tb_frame.f_code.co_name])
        cursor = cursor.tb_next
    print(json.dumps({"same": value is EXPECTED, "traceback_same": value.__traceback__ is tb, "frames": frames}))
sys.excepthook = hook
def fail():
    raise EXPECTED
fail()
''',
    'hook-error': '''import sys
def hook(kind, value, tb):
    raise RuntimeError("original hook failure")
sys.excepthook = hook
raise ValueError("original error")
''',
    'interrupt': 'raise KeyboardInterrupt\n',
    'exit': 'raise SystemExit(17)\n',
}


class PythonUncaughtTests(unittest.TestCase):
    @unittest.skipUnless(CLI.is_file(), 'make python-check builds the native CLI')
    def test_selected_unhandled_error_preserves_output_and_exports_completed_error_spans(self):
        source = 'def fail():\n    raise ValueError("original failure")\ndef outer():\n    fail()\nouter()\n'
        with tempfile.TemporaryDirectory() as name, receiver() as (endpoint, requests):
            folder = Path(name)
            script = folder / 'entry.py'; script.write_text(source)
            config = folder / 'policy.toml'
            config.write_text('schema_version=2\nlanguages=["python"]\n[sources]\ninclude=["entry.py"]\n'
                              '[functions]\ninclude=["entry.outer", "entry.fail"]\n'
                              '[traces]\nenabled=true\nroot_sample_ratio=1.0\n[export]\n'
                              f'endpoint={json.dumps(endpoint)}\ntimeout_ms=500\n')
            original_config = config.read_bytes()
            report = folder / 'report.json'
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('OTEL_', 'OTELC_'))}
            environment['OTELC_REPORT_PATH'] = str(report)
            self.compare(folder, config, [script.name], environment)
            health = json.loads(report.read_text())
            self.assertEqual(health['function_calls'], 2)
            self.assertFalse(health['losses']['incomplete'])
            self.assertFalse(health['export_loss'])
            self.assertTrue(health['export_finished'])
            self.assertFalse(health['traces']['losses'])
            spans, calls = [], {}
            for path, _, body in requests:
                if path == '/v1/traces':
                    spans.extend(span for resource in ExportTraceServiceRequest.FromString(body).resource_spans
                                 for scope in resource.scope_spans for span in scope.spans)
                else:
                    self.assertEqual(path, '/v1/metrics')
                    for resource in ExportMetricsServiceRequest.FromString(body).resource_metrics:
                        for scope in resource.scope_metrics:
                            for metric in scope.metrics:
                                if metric.name == 'otelc.function.calls':
                                    for point in metric.sum.data_points:
                                        function = next(value.value.string_value for value in point.attributes
                                                        if value.key == 'code.function.name')
                                        calls[function] = point.as_int
            self.assertEqual(calls, {'entry.fail': 1, 'entry.outer': 1})
            self.assertEqual({span.name for span in spans}, {'entry.fail', 'entry.outer'})
            self.assertEqual(len(spans), 2)
            self.assertTrue(all(span.status.code == 2 for span in spans))
            outer = next(span for span in spans if span.name == 'entry.outer')
            child = next(span for span in spans if span.name == 'entry.fail')
            self.assertEqual(child.trace_id, outer.trace_id)
            self.assertEqual(child.parent_span_id, outer.span_id)
            self.assertFalse(outer.parent_span_id)
            self.assertEqual(script.read_text(), source)
            self.assertEqual(config.read_bytes(), original_config)

    def test_trim_prefix_uses_code_identity_without_exception_hooks(self):
        accesses = []

        class OriginalError(Exception):
            def __getattribute__(self, name):
                if name == '__traceback__':
                    accesses.append('get')
                return super().__getattribute__(name)

            def __setattr__(self, name, value):
                if name == '__traceback__':
                    accesses.append('set')
                return super().__setattr__(name, value)

        error = OriginalError('kept')

        def application_failure():
            raise error

        def launch_call():
            application_failure()

        try:
            launch_call()
        except OriginalError as caught:
            descriptor = BaseException.__dict__['__traceback__']
            original = descriptor.__get__(caught, type(caught))
            application_tail = original.tb_next.tb_next
            self.assertIs(application_tail.tb_frame.f_code, application_failure.__code__)
            self.assertEqual(original.tb_frame.f_code.co_filename,
                             application_tail.tb_frame.f_code.co_filename)
            owned_codes = (self.test_trim_prefix_uses_code_identity_without_exception_hooks.__code__,
                           launch_call.__code__)
            launch.trim_launch_prefix(caught, owned_codes)
            self.assertIs(caught, error)
            self.assertIs(descriptor.__get__(caught, type(caught)), application_tail)
        self.assertEqual(accesses, [])

    def compare(self, folder: Path, config: Path, arguments: list[str], environment: dict[str, str]) -> None:
        baseline = subprocess.run([sys.executable, *arguments], cwd=folder, env=environment,
                                  capture_output=True, timeout=15)
        measured = subprocess.run([str(ROOT / 'target/debug/quux-otelc'), '--config', str(config),
                                  'python', *arguments], cwd=folder,
                                 env=dict(environment, OTELC_PYTHON=sys.executable,
                                          OTELC_ADAPTER_ROOT=str(ROOT / 'adapters')),
                                 capture_output=True, timeout=15)
        self.assertEqual(measured.returncode, baseline.returncode)
        self.assertEqual(measured.stdout, baseline.stdout)
        self.assertEqual(measured.stderr, baseline.stderr)

    @unittest.skipUnless(CLI.is_file(), 'make python-check builds the native CLI')
    def test_exception_getters_and_setters_are_not_dispatched_by_traceback_cleanup(self):
        source = '''import json, sys
GETS = SETS = 0
class OriginalError(Exception):
    def __getattribute__(self, name):
        global GETS
        if name == '__traceback__': GETS += 1
        return super().__getattribute__(name)
    def __setattr__(self, name, value):
        global SETS
        if name == '__traceback__': SETS += 1
        return super().__setattr__(name, value)
EXPECTED = OriginalError('kept')
def hook(kind, value, tb):
    same = value.__traceback__ is tb
    frames = []
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    print(json.dumps({'same': same, 'identity': value is EXPECTED, 'gets': GETS, 'sets': SETS, 'frames': frames}))
sys.excepthook = hook
raise EXPECTED
'''
        with tempfile.TemporaryDirectory() as name, receiver() as (endpoint, _):
            folder = Path(name)
            config = folder / 'policy.toml'
            config.write_text('schema_version=2\nlanguages=["python"]\n[metrics]\nenabled=false\n'
                              f'[export]\nendpoint={json.dumps(endpoint)}\n')
            script = folder / 'entry.py'; script.write_text(source)
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('OTEL_', 'OTELC_'))}
            for arguments in ([script.name], ['-m', 'entry']):
                with self.subTest(arguments=arguments):
                    self.compare(folder, config, arguments, environment)

    @unittest.skipUnless(CLI.is_file(), 'make python-check builds the native CLI')
    def test_syntax_error_matches_interpreter_and_setup_failure_remains_diagnostic(self):
        with tempfile.TemporaryDirectory() as name, receiver() as (endpoint, _):
            folder = Path(name)
            config = folder / 'policy.toml'
            config.write_text('schema_version=2\nlanguages=["python"]\n[metrics]\nenabled=false\n'
                              f'[export]\nendpoint={json.dumps(endpoint)}\n')
            script = folder / 'entry.py'; script.write_text('def fail(:\n    pass\n')
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('OTEL_', 'OTELC_'))}
            self.compare(folder, config, [script.name], environment)
            script.write_text('print("application reached")\n')
            actual = subprocess.run([str(ROOT / 'target/debug/quux-otelc'), '--config', str(config),
                                     'python', script.name], cwd=folder,
                                    env=dict(environment, OTELC_PYTHON=sys.executable,
                                             OTELC_ADAPTER_ROOT=str(ROOT / 'adapters'),
                                             OTEL_EXPORTER_OTLP_METRICS_COMPRESSION='unsupported-value'),
                                    capture_output=True, timeout=15)
            self.assertNotEqual(actual.returncode, 0)
            self.assertFalse(actual.stdout)
            self.assertIn(b'launch.py', actual.stderr)
            self.assertIn(b'unsupported OTLP metrics compression', actual.stderr)

    @unittest.skipUnless(CLI.is_file(), 'make python-check builds the native CLI')
    def test_unhandled_errors_and_hooks_match_all_supported_valid_entry_routes(self):
        self.assertTrue((ROOT / 'target/debug/quux-otelc').is_file(), 'build the real CLI first')
        with tempfile.TemporaryDirectory(prefix='otelc uncaught ') as name, receiver() as (endpoint, _):
            folder = Path(name)
            config = folder / 'policy.toml'
            config.write_text('schema_version=2\nlanguages=["python"]\n[sources]\ninclude=[]\n'
                              '[functions]\ninclude=[]\n[metrics]\nenabled=false\n[export]\n'
                              f'endpoint={json.dumps(endpoint)}\ntimeout_ms=500\n')
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('OTEL_', 'OTELC_'))}
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            package = folder / 'package'; package.mkdir()
            (package / '__init__.py').write_text('')
            directory = folder / 'directory'; directory.mkdir()
            for case, source in ERRORS.items():
                script = folder / 'entry.py'; script.write_text(source)
                bytecode = folder / 'entry.pyc'
                py_compile.compile(str(script), cfile=str(bytecode), doraise=True)
                (package / '__main__.py').write_text(source)
                (directory / '__main__.py').write_text(source)
                archive = folder / 'application.zip'
                with zipfile.ZipFile(archive, 'w') as packed:
                    packed.writestr('__main__.py', source)
                original = {path: path.read_bytes() for path in
                            (script, bytecode, package / '__main__.py', directory / '__main__.py', archive)}
                for arguments in ([script.name], [bytecode.name], ['-m', 'entry'],
                                  ['-m', 'package'], [directory.name], [archive.name]):
                    with self.subTest(case=case, arguments=arguments):
                        self.compare(folder, config, arguments, environment)
                self.assertEqual({path: path.read_bytes() for path in original}, original)
