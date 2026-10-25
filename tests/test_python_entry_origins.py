"""Actual native entry roots and local VM executions retain original app behaviour."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import CodeType

from ci import build_python_inputs as tooling

from python_input_support import NativeInputCase
from otelc import python_origin_journal as journal
from otelc.python_code_descriptor import encode


class PythonEntryOriginTests(NativeInputCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.tool = tooling.build(coverage=bool(os.environ.get('OTELC_NATIVE_COVERAGE')), origin_entry=True)
        cls.build_record = json.loads((cls.tool.parent / 'build.json').read_text())
        root = Path(__file__).resolve().parents[1]
        cls.backend = root / 'build/native' / ('python-origin-backend-' + sys.implementation.cache_tag)
        _, flags = tooling.toolchain()
        profile = ['-fprofile-instr-generate', '-fcoverage-mapping'] if os.environ.get('OTELC_NATIVE_COVERAGE') else []
        with tempfile.TemporaryDirectory(dir=root / 'build/native') as temporary:
            candidate = Path(temporary) / 'backend'
            subprocess.run([tooling.compiler(), '-std=c11', '-Wall', '-Wextra', '-Werror',
                str(root / 'tests/fixtures/python_origin_backend.c'),
                str(cls.tool.parent / cls.build_record['library']),
                '-I' + str(root / 'native/python'), '-I' + str(cls.tool.parent),
                '-Wl,-rpath,' + str(cls.tool.parent), '-o', str(candidate), *profile, *flags], check=True)
            os.replace(candidate, cls.backend)
        if cls.build_record['inputs']['origin_chain']['available']:
            cls.controls = root / 'build/native' / ('python-origin-events-' + sys.implementation.cache_tag + '.dylib')
            _, flags = tooling.toolchain()
            profile = ['-fprofile-instr-generate', '-fcoverage-mapping'] if os.environ.get('OTELC_NATIVE_COVERAGE') else []
            with tempfile.TemporaryDirectory(dir=root / 'build/native') as temporary:
                candidate = Path(temporary) / 'controls.dylib'
                subprocess.run([tooling.compiler(), '-dynamiclib', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    str(root / 'tests/fixtures/python_origin_events.c'), '-I' + str(cls.tool.parent),
                    '-o', str(candidate), *profile, *flags], check=True)
                os.replace(candidate, cls.controls)

    def require_profile(self) -> None:
        record = json.loads((self.tool.parent / 'build.json').read_text())
        if not record['inputs']['origin_chain']['available']:
            self.skipTest('the selected native entry image has no supported call map')

    def test_native_image_parser_boundaries_and_arbitrary_caller_refusal(self) -> None:
        result = subprocess.run([str(self.backend)], capture_output=True, timeout=15)
        expected = b'supported\n' if self.build_record['inputs']['origin_chain']['available'] else b'unavailable\n'
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, b''))

    def test_unavailable_profile_cannot_claim_capture_success(self) -> None:
        record = json.loads((self.tool.parent / 'build.json').read_text())
        if record['inputs']['origin_chain']['available']:
            self.skipTest('the selected native entry image is supported')
        entry = self.root / 'original.py'
        entry.write_text('print("original")\n')
        status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True)
        self.assertEqual((status, out, err), (0, b'original\n', b''))
        value = journal.read(path, expected_pid=pid)
        self.assertFalse(value.capture_complete)
        self.assertEqual(value.roots, ())

    def test_actual_root_const_edges_optimisation_and_original_channels(self) -> None:
        self.require_profile()
        entry = self.root / 'original.py'
        data = b'"module docs"\ndef chosen(value):\n    "function docs"\n    return value*2+3\nprint(chosen(7))\n'
        entry.write_bytes(data)
        for optimize in (0, 1, 2):
            flags = ['-B'] + (['-' + 'O' * optimize] if optimize else [])
            stock = subprocess.run([sys.executable, *flags, str(entry)], capture_output=True, timeout=15)
            status, out, err, path, pid = self.launch([*flags, str(entry)], entry_origins=True)
            self.assertEqual((status, out, err), (stock.returncode, stock.stdout, stock.stderr))
            value = journal.read(path, expected_pid=pid)
            self.assertTrue(value.capture_complete)
            self.assertEqual((len(value.roots), len(value.executions), value.sdk_joins), (1, 2, ()))
            root = value.roots[0]
            self.assertEqual((root.route, root.loader, root.compiler_flags, root.optimize), (1, 0, 0, optimize))
            self.assertEqual(root.descriptor.sha256, hashlib.sha256(encode(compile(
                data, str(entry), 'exec', dont_inherit=True, optimize=optimize))).hexdigest())
            self.assertEqual(value.executions[0].edges, ())
            self.assertTrue(value.executions[1].edges)
            self.assertEqual(entry.read_bytes(), data)

    def test_selected_code_can_first_execute_on_an_ordinary_worker(self) -> None:
        self.require_profile()
        entry = self.root / 'worker.py'
        entry.write_text('import threading\ndef chosen():\n    print("worker")\nt=threading.Thread(target=chosen)\nt.start()\nt.join()\n')
        status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True)
        self.assertEqual((status, out, err), (0, b'worker\n', b''))
        value = journal.read(path, expected_pid=pid)
        self.assertTrue(value.capture_complete)
        self.assertEqual((len(value.roots), len(value.executions)), (1, 2))

    def test_native_public_private_and_file_reentry_cannot_borrow_the_read(self) -> None:
        self.require_profile()
        entry = self.root / 'original.py'
        entry.write_text('def chosen(value):\n    return value*2+3\nprint(chosen(7))\n')
        combinations = list(itertools.product(('audit_only', 'private', 'file', 'execute'), ('compile', 'exec')))
        combinations += [('equal_' + mode, event) for mode, event in
                         itertools.product(('audit_only', 'private'), ('compile', 'exec'))]
        for index, (mode, event) in enumerate(combinations):
            report = self.root / ('control-%d.json' % index)
            environment = {**os.environ, 'DYLD_INSERT_LIBRARIES': str(self.controls),
                'OTELC_RESEARCH_ENTRY': str(entry), 'OTELC_RESEARCH_TRIGGER': event,
                'OTELC_RESEARCH_SPOOF': mode.removeprefix('equal_'),
                'OTELC_RESEARCH_CONTROL_REPORT': str(report)}
            if mode.startswith('equal_'):
                environment['OTELC_RESEARCH_EQUAL'] = '1'
            with self.subTest(mode=mode, event=event):
                status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True,
                    environment=environment)
                self.assertEqual((status, out, err), (0, b'17\n', b''))
                value = journal.read(path, expected_pid=pid)
                self.assertTrue(value.capture_complete)
                self.assertEqual((len(value.roots), len(value.executions)), (1, 2))
                self.assertEqual(value.roots[0].descriptor.sha256, hashlib.sha256(encode(compile(
                    entry.read_bytes(), str(entry), 'exec', dont_inherit=True))).hexdigest())
                control = json.loads(report.read_text())
                self.assertTrue(control['triggered'])
                if mode == 'file':
                    self.assertEqual(control['file_runs'], 1)
                else:
                    self.assertEqual(control['compiled'], 2)
                    self.assertEqual(control['private_events' if mode.endswith('private') else 'public_events'], 2)

    def test_encodings_errors_and_exit_codes_match_stock(self) -> None:
        self.require_profile()
        sources = [b'\xef\xbb\xbfprint("bom")\n', b'# coding: latin-1\nprint("caf\xe9")\n',
                   b'from __future__ import annotations\ndef chosen(value: MissingType): return value\nprint(chosen(7))\n',
                   b'raise ValueError("original marker")\n', b'def syntax(:\n', b'raise SystemExit(7)\n', b'']
        for index, source in enumerate(sources):
            entry = self.root / ('original-%d.py' % index)
            entry.write_bytes(source)
            stock = subprocess.run([sys.executable, '-B', str(entry)], capture_output=True, timeout=15)
            with self.subTest(source=index):
                status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True)
                self.assertEqual((status, out, err), (stock.returncode, stock.stdout, stock.stderr))
                value = journal.read(path, expected_pid=pid)
                self.assertTrue(value.capture_complete)
                self.assertEqual(len(value.roots), 0 if index == 4 else 1)
                self.assertEqual(entry.read_bytes(), source)

    def test_local_registration_leaves_unrelated_code_and_global_events_alone(self) -> None:
        self.require_profile()
        entry = self.root / 'local.py'
        entry.write_text('import sys, json\ntool=next(i for i in range(6) if sys.monitoring.get_tool(i)=="quux.otelc.origins")\nother=compile("pass", "unrelated.py", "exec")\nprint(json.dumps([sys.monitoring.get_events(tool), sys.monitoring.get_local_events(tool,other)]))\n')
        status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True)
        self.assertEqual((status, out, err), (0, b'[0, 0]\n', b''))
        self.assertTrue(journal.read(path, expected_pid=pid).capture_complete)

    def test_monitoring_replacement_or_no_free_id_preserves_app_and_reports_loss(self) -> None:
        self.require_profile()
        entry = self.root / 'original.py'
        entry.write_text('print("original")\n')
        hook = self.root / 'sitecustomize.py'
        changes = ['sys.monitoring.set_local_events=lambda *args: None',
                   'sys.monitoring.DISABLE=object()',
                   'for i in (2,3,4,5): sys.monitoring.use_tool_id(i,"application")']
        for change in changes:
            hook.write_text('import sys\n' + change + '\n')
            status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True,
                environment={**os.environ, 'PYTHONPATH': str(self.root)})
            self.assertEqual((status, out, err), (0, b'original\n', b''))
            value = journal.read(path, expected_pid=pid)
            self.assertFalse(value.capture_complete)
            self.assertEqual(value.roots, ())

    def test_encoder_capacity_failure_releases_its_own_tool_without_discarding_app(self) -> None:
        self.require_profile()
        entry = self.root / 'large.py'
        entry.write_text('value="' + 'x' * (2 * 1024 * 1024) + '"\nimport sys\n'
                         'print(len(value), any(sys.monitoring.get_tool(i)=="quux.otelc.origins" for i in range(6)))\n')
        status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True)
        self.assertEqual((status, out, err), (0, b'2097152 False\n', b''))
        value = journal.read(path, expected_pid=pid)
        self.assertFalse(value.capture_complete)
        self.assertEqual(value.roots, ())

    def test_entry_input_capacity_failure_releases_an_inactive_monitoring_slot(self) -> None:
        self.require_profile()
        entry = self.root / 'unrepresentable.py'
        entry.write_text('#' + 'x' * (17 * 1024 * 1024) + '\nimport sys\nprint(sys.monitoring.get_tool(2))\n')
        status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True)
        self.assertEqual((status, out, err), (0, b'None\n', b''))
        value = journal.read(path, expected_pid=pid)
        self.assertFalse(value.capture_complete)
        self.assertEqual(value.capture_failures, 1)
        self.assertEqual(value.roots, ())

    def test_command_mode_retains_original_behaviour_without_claiming_an_entry_root(self) -> None:
        status, out, err, path, pid = self.launch(['-B', '-c', 'print("original command")'], entry_origins=True)
        self.assertEqual((status, out, err), (0, b'original command\n', b''))
        value = journal.read(path, expected_pid=pid)
        self.assertFalse(value.capture_complete)
        self.assertEqual(value.roots, ())

    def test_manual_callback_cannot_claim_that_an_unexecuted_code_object_started(self) -> None:
        self.require_profile()
        entry = self.root / 'manual.py'
        entry.write_text('import sys\ndef chosen(): print("real call")\n'
                         'tool=next(i for i in range(6) if sys.monitoring.get_tool(i)=="quux.otelc.origins")\n'
                         'callback=sys.monitoring.register_callback(tool,sys.monitoring.events.PY_START,None)\n'
                         'callback(chosen.__code__,0)\n'
                         'sys.monitoring.register_callback(tool,sys.monitoring.events.PY_START,callback)\n'
                         'print("original")\n')
        root = compile(entry.read_bytes(), str(entry), 'exec', dont_inherit=True)
        chosen_edge = next(index for index, value in enumerate(root.co_consts)
                           if type(value) is CodeType and value.co_name == 'chosen')
        status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True)
        self.assertEqual((status, out, err), (0, b'original\n', b''))
        value = journal.read(path, expected_pid=pid)
        self.assertFalse(value.capture_complete)
        self.assertEqual((len(value.roots), len(value.executions)), (1, 2))
        self.assertFalse(any(execution.edges == (chosen_edge,) for execution in value.executions))
