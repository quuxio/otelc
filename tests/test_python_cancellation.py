"""Public unchanged-source cancellation and hostile exception preservation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]
SOURCE = '''import asyncio, json
from asyncio.exceptions import CancelledError
reads = {'instance': 0, 'type': 0, 'format': 0}
class HostileMeta(type):
    def __eq__(cls, other):
        reads['type'] += 1
        raise AssertionError('application type equality must not run')
    __hash__ = type.__hash__
    def __getattribute__(cls, name):
        if name in ('__mro__', '__qualname__'):
            reads['type'] += 1
            raise AssertionError('application type hook must not run')
        return type.__getattribute__(cls, name)
class CountingError(Exception, metaclass=HostileMeta):
    @property
    def __class__(self):
        reads['instance'] += 1
        return Exception
    def __str__(self):
        reads['format'] += 1
        raise AssertionError('application formatter must not run')
    __repr__ = __str__
class RaisingError(CountingError):
    @property
    def __class__(self):
        reads['instance'] += 1
        raise AssertionError('application class getter must not run')
class SpoofingError(CountingError):
    @property
    def __class__(self):
        reads['instance'] += 1
        return CancelledError
class CancelSubclass(CancelledError, metaclass=HostileMeta):
    @property
    def __class__(self):
        reads['instance'] += 1
        return Exception
class ExitSubclass(GeneratorExit, metaclass=HostileMeta):
    @property
    def __class__(self):
        reads['instance'] += 1
        return Exception
def workload(error):
    raise error
identities = []
for error in (CountingError(), RaisingError(), SpoofingError(), CancelledError(),
              CancelSubclass(), GeneratorExit(), ExitSubclass()):
    try:
        workload(error)
    except BaseException as caught:
        identities.append(caught is error)
async def workload_task():
    await asyncio.sleep(3600)
async def run_task():
    task = asyncio.create_task(workload_task())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except CancelledError:
        return task.cancelled()
cancelled = asyncio.run(run_task())
closed = []
def workload_generator():
    try:
        yield 7
    finally:
        closed.append(True)
generator = workload_generator()
yielded = next(generator)
generator.close()
print(json.dumps({'reads': reads, 'identities': identities, 'cancelled': cancelled,
                  'closed': closed, 'yielded': yielded}, sort_keys=True))
'''


class PythonCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='otelc-python-cancellation-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.application = self.base / 'application'
        self.application.mkdir()
        self.entry = self.application / 'entry.py'
        self.entry.write_text(SOURCE)

    def test_public_cli_preserves_exception_identity_and_marks_only_real_cancellation(self) -> None:
        expected = {'reads': {'instance': 0, 'type': 0, 'format': 0}, 'identities': [True]*7,
                    'cancelled': True, 'closed': [True], 'yielded': 7}
        source_hash = hashlib.sha256(self.entry.read_bytes()).hexdigest()
        for name, metrics, traces in (('metrics', True, False), ('traces', False, True), ('combined', True, True)):
            with self.subTest(signals=name):
                self.run_pair(name, metrics, traces, expected, source_hash)

    def run_pair(self, name: str, metrics: bool, traces: bool, expected: dict, source_hash: str) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        configuration = self.base / (name + '.toml')
        configuration.write_text(f'''schema_version = 3
languages = ["python"]
[sources]
include = ["**/*.py"]
[functions]
include = ["*workload*"]
[metrics]
collection = {str(metrics).lower()}
emission = {str(metrics).lower()}
[traces]
enabled = {str(traces).lower()}
sample_ratio = 1.0
[export]
endpoint = "http://127.0.0.1:{capture.server.server_port}"
interval_ms = 60000
timeout_ms = 1000
shutdown_timeout_ms = 5000
''')
        config_hash = hashlib.sha256(configuration.read_bytes()).hexdigest()
        results = []
        for lane in ('plain', 'instrumented'):
            runtime = self.base / (name + '-' + lane)
            command = [str(ROOT / 'bin/quux-otelc'), '--config', str(configuration),
                'python', '--root', str(self.application), '--entry', 'entry.py', '--runtime', str(runtime)]
            if lane == 'plain':
                command.append('--plain')
            result = subprocess.run(command, cwd=ROOT, env=os.environ | {'OTELC_PYTHON': sys.executable},
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual((result.returncode, result.stderr), (0, ''), result.stdout)
            self.assertEqual(json.loads(result.stdout), expected)
            if lane == 'instrumented':
                final = json.loads((runtime / 'sdk/python-sdk-status.json').read_text())
                self.assertEqual(final['phase'], 'closed')
                self.assertEqual(final['producer']['function_completions'], 9)
                self.assertNotEqual(final['pid'], final['sdk_pid'])
            results.append((result.returncode, result.stdout, result.stderr))
            self.assertEqual(hashlib.sha256(self.entry.read_bytes()).hexdigest(), source_hash)
            self.assertEqual(hashlib.sha256(configuration.read_bytes()).hexdigest(), config_hash)
        self.assertEqual(results[0], results[1])
        spans = [span for message in capture.traces for resource in message.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans), 9 if traces else 0)
        for span in spans:
            self.assertEqual(span.status.code, 2)
            attrs = {attribute.key: attribute.value for attribute in span.attributes}
            error_types = [attribute.value.string_value for event in span.events for attribute in event.attributes
                           if event.name == 'exception' and attribute.key == 'exception.type']
            self.assertEqual(len(error_types), 1)
            cancelled = error_types[0] in ('CancelledError', 'CancelSubclass', 'GeneratorExit', 'ExitSubclass')
            self.assertEqual('otelc.cancelled' in attrs, cancelled, error_types[0])
            if cancelled:
                self.assertIs(attrs['otelc.cancelled'].bool_value, True)
                self.assertEqual(span.status.message, 'cancelled')
        calls = capture.points('otelc.function.calls')
        if metrics:
            by_function = {}
            for point in calls:
                function = next(attr.value.string_value for attr in point.attributes if attr.key == 'function')
                by_function[function] = max(by_function.get(function, 0), point.as_int)
            self.assertEqual(sum(by_function.values()), 9)
            exceptions = capture.points('otelc.function.exceptions')
            by_function = {}
            for point in exceptions:
                function = next(attr.value.string_value for attr in point.attributes if attr.key == 'function')
                by_function[function] = max(by_function.get(function, 0), point.as_int)
            self.assertEqual(sum(by_function.values()), 9)
        else:
            self.assertEqual(calls, [])
