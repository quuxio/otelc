"""Public origin-profile launches preserve ordinary applications in both lanes."""
from __future__ import annotations

import copy
import json
import os
import py_compile
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ci import build_python_inputs as tooling
from otelc import prepared_python as prepared, python_recipe as recipe, control
from otelc import python_launch_files as files
from otelc import python_origin_execution as execution
from otelc import python_origin_journal as journal
from otelc.config import Policy
from test_java_integration import Capture

PROFILE = 'python-native-origins-v2'


class PreparedPythonOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = mock.patch.dict(os.environ, {'PYTHONDONTWRITEBYTECODE': '1'})
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / 'app'
        self.app.mkdir()
        self.entry = self.app / 'entry.py'
        self.entry.write_text('def workload(value):return value*2+3\nprint(workload(7))\n')
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['python'],
            'sources': {'include': ['*.py']}, 'functions': {'include': ['*.workload']},
            'metrics': {'collection': True, 'emission': True},
            'traces': {'enabled': True, 'sample_ratio': 1.0},
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 100000, 'timeout_ms': 1000}})
        self.counter = 0

    def prepare(self, *, plain: bool = False) -> tuple[Path, dict]:
        inputs, _ = tooling.toolchain(origin_entry=True)
        if not inputs['origin_chain']['available']:
            self.skipTest('the selected native source image is unavailable')
        self.counter += 1
        runtime = self.root / f'runtime-{self.counter}'
        result = prepared.prepare(self.policy, runtime, root=self.app,
                                  entry='entry.py', plain=plain, profile=PROFILE)
        return runtime, result

    def test_explicit_formats_and_prepare_bind_root_without_starting_application(self) -> None:
        legacy = prepared.protocols()
        origin = prepared.protocols(profile=PROFILE)
        self.assertEqual(legacy['schema_version'], 1)
        self.assertEqual(legacy['journal']['schema_version'], 1)
        self.assertEqual(origin['schema_version'], 2)
        self.assertEqual(origin['profile'], PROFILE)
        self.assertEqual(origin['journal'], {'schema_version': 2, 'record_kinds': list(range(1, 8))})
        self.assertFalse(origin['source_complete'])
        self.assertIsNone(origin['code_comparisons'])
        runtime, result = self.prepare()
        for name in ('request', 'plan', 'prepared'):
            self.assertEqual(result[name]['schema_version'], 2)
        self.assertEqual(result['schema_version'], 2)
        self.assertEqual(result['request']['profile'], PROFILE)
        self.assertEqual(prepared.verify(runtime), (result['request'], result['plan'], result['prepared']))
        command = recipe.native_command(result['request'], prepared.native_tool(PROFILE), 17)
        self.assertEqual(command[1:4], ['--origin-loader-root', str(self.app), '17'])
        self.assertFalse((runtime / 'sdk').exists())
        self.assertFalse((runtime / 'python-inputs.journal').exists())
        changed = copy.deepcopy(result['request'])
        changed['schema_version'] = 1
        with self.assertRaises(ValueError):
            recipe.resolve_plan(changed)

    def test_unchanged_entry_fresh_import_and_stdlib_have_origins_in_both_lanes(self) -> None:
        target = self.app / 'target.py'
        target.write_text('def workload(value):return value*2+3\n')
        self.entry.write_text('import fractions\nfrom target import workload\nprint(workload(7))\n')
        original = {path.name: path.read_bytes() for path in (self.entry, target)}
        for plain in (True, False):
            with self.subTest(plain=plain):
                runtime, result = self.prepare(plain=plain)
                child = subprocess.run(result['command'], capture_output=True, timeout=30)
                self.assertEqual((child.returncode, child.stdout, child.stderr), (0, b'17\n', b''))
                report = json.loads((runtime / 'python-execution.json').read_bytes())
                self.assertEqual((report['schema_version'], report['phase']), (2, 'closed'))
                self.assertFalse(report['source_complete'])
                self.assertEqual(report['entry_evidence']['phase'], 'complete')
                value = journal.read(runtime / 'python-inputs.journal', expected_pid=report['application']['pid'])
                self.assertTrue(value.capture_complete)
                self.assertEqual(len(value.roots), 2)
                self.assertEqual(len(value.sdk_joins), 0 if plain else 1)
                self.assertEqual({path.name: path.read_bytes() for path in (self.entry, target)}, original)
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans), 1)
        attributes = {item.key: item.value for item in spans[0].attributes}
        self.assertEqual(attributes['otelc.python.code_id'].int_value, value.sdk_joins[0][0])
        self.assertEqual(self.capture.calls(), 1)

    def test_cached_application_refuses_complete_evidence_with_original_output(self) -> None:
        target = self.app / 'target.py'
        target.write_text('def workload(value):return value*2+3\n')
        py_compile.compile(str(target), doraise=True)
        self.entry.write_text('from target import workload\nprint(workload(7))\n')
        for plain in (True, False):
            with self.subTest(plain=plain):
                runtime, result = self.prepare(plain=plain)
                child = subprocess.run(result['command'], capture_output=True, timeout=30)
                self.assertEqual((child.returncode, child.stdout, child.stderr), (1, b'17\n', b''))
                report = json.loads((runtime / 'python-execution.json').read_bytes())
                self.assertEqual(report['application']['returncode'], 0)
                self.assertEqual(report['phase'], 'failed')
                self.assertEqual(report['entry_evidence']['phase'], 'failed')
                self.assertFalse(report['source_complete'])
                observed = execution.read(runtime / 'python-inputs.journal',
                    expected_pid=report['application']['pid'], entry=result['prepared']['entry'],
                    root=self.app, plain=plain)
                self.assertIn('native_capture', observed['origins']['failures'])
                if not plain:
                    self.assertIn('selected_sdk_origins', observed['origins']['failures'])

    def test_origin_capture_and_code_id_survive_all_live_metric_states(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['metrics'].update(collection=False, emission=False)
        self.policy = Policy.parse(data)
        self.entry.write_text('''import sys
def workload(value):return value*2+3
for line in sys.stdin:
    if line.strip()=='quit':break
    print(workload(int(line)),flush=True)
''')
        runtime, result = self.prepare()
        process = subprocess.Popen(result['command'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        pids = set()
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    application, _ = files.read(runtime / 'python-application-start.json')
                    break
                except FileNotFoundError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('application startup record unavailable')
                    time.sleep(.01)
            endpoint = runtime / 'control.json'
            control.wait_ready(endpoint, application['application_pid'])
            for index, (collection, emission) in enumerate(((False, False), (True, False),
                    (False, True), (True, True), (False, False))):
                state = control.request(endpoint, 'collection ' + ('on' if collection else 'off'))
                pids.add(state['pid'])
                state = control.request(endpoint, 'emission ' + ('on' if emission else 'off'))
                self.assertEqual((state['collection'], state['emission']), (collection, emission))
                process.stdin.write(str(index).encode() + b'\n')
                process.stdin.flush()
                self.assertEqual(process.stdout.readline(), str(index*2+3).encode() + b'\n')
                control.request(endpoint, 'flush')
            process.stdin.write(b'quit\n')
            process.stdin.flush()
            out, err = process.communicate(timeout=30)
            self.assertEqual((process.returncode, out, err), (0, b'', b''))
        finally:
            if process.poll() is None:
                prepared.stop_group(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        report = json.loads((runtime / 'python-execution.json').read_bytes())
        self.assertEqual(report['phase'], 'closed')
        self.assertEqual(pids, {report['application']['pid']})
        value = journal.read(runtime / 'python-inputs.journal', expected_pid=report['application']['pid'])
        self.assertEqual(len(value.sdk_joins), 1)
        self.assertEqual(len(value.codes), 1)
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans), 5)
        for span in spans:
            attributes = {item.key: item.value for item in span.attributes}
            self.assertEqual(attributes['otelc.python.code_id'].int_value, value.sdk_joins[0][0])
        self.assertEqual(self.capture.calls(), 2)

    def test_origin_summary_refuses_wrong_entry_root_or_baseline_role(self) -> None:
        runtime, result = self.prepare()
        child = subprocess.run(result['command'], capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 0)
        report = json.loads((runtime / 'python-execution.json').read_bytes())
        arguments = {'expected_pid': report['application']['pid'], 'entry': result['prepared']['entry'],
                     'root': self.app, 'plain': False}
        self.assertEqual(execution.read(runtime / 'python-inputs.journal', **arguments)['phase'], 'complete')
        for expected, changes in (('entry_origin', {'entry': {**arguments['entry'], 'sha256': '0'*64}}),
                                  ('root_scope', {'root': self.root / 'other'}),
                                  ('baseline_sdk_observations', {'plain': True})):
            with self.subTest(expected=expected):
                value = execution.read(runtime / 'python-inputs.journal', **{**arguments, **changes})
                self.assertEqual(value['phase'], 'failed')
                self.assertIn(expected, value['origins']['failures'])

    def test_unknown_profile_and_module_origin_profile_refuse_before_execution(self) -> None:
        with self.assertRaises(ValueError):
            prepared.protocols(profile='unverified-profile')
        with self.assertRaises(ValueError):
            prepared.prepare(self.policy, self.root / 'bad', root=self.app,
                             entry='entry.py', profile='unverified-profile')
        self.assertFalse((self.root / 'bad').exists())
        with self.assertRaisesRegex(ValueError, 'source entry'):
            prepared.prepare(self.policy, self.root / 'module', root=self.app,
                             module='entry', profile=PROFILE)
        with mock.patch.object(tooling, 'toolchain', return_value=({'origin_chain': {'available': False}}, [])):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                prepared.native_tool(PROFILE)
