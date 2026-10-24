"""Application-root capture ignores other loaders without admitting cached app code."""

from __future__ import annotations

import json
import os
import py_compile
from pathlib import Path

from ci import build_python_inputs as tooling
from otelc import python_origin_journal as journal
from python_input_support import NativeInputCase


class PythonOriginSourceScopeTests(NativeInputCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.tool = tooling.build(coverage=bool(os.environ.get('OTELC_NATIVE_COVERAGE')), origin_entry=True)

    def setUp(self) -> None:
        super().setUp()
        metadata = json.loads((self.tool.parent / 'build.json').read_bytes())
        if not metadata['inputs']['origin_chain']['available']:
            self.skipTest('the selected native source image is unavailable')
        self.application = self.root / 'application'
        self.application.mkdir()
        self.entry = self.application / 'entry.py'
        self.target = self.application / 'target.py'
        self.target.write_text('def workload(value):return value*2+3\n')

    def run_application(self, body: str, *, root: Path | None = None) -> journal.OriginJournal:
        self.entry.write_text(body)
        status, out, err, path, pid = self.launch(['-B', str(self.entry)],
                                                origin_root=root or self.application)
        self.assertEqual((status, out, err), (0, b'17\n', b''))
        return journal.read(path, expected_pid=pid)

    def test_cached_standard_library_does_not_invalidate_fresh_application_source(self) -> None:
        value = self.run_application('import fractions\nfrom target import workload\nprint(workload(7))\n')
        self.assertTrue(value.capture_complete)
        self.assertEqual(len(value.roots), 2)
        self.assertEqual({os.fsdecode(root.descriptor.path) for root in value.roots},
                         {str(self.entry), str(self.target)})

    def test_cached_application_code_still_marks_capture_incomplete(self) -> None:
        py_compile.compile(str(self.target), doraise=True)
        value = self.run_application('from target import workload\nprint(workload(7))\n')
        self.assertFalse(value.capture_complete)
        self.assertEqual(len(value.roots), 1)

    def test_sibling_prefix_and_symlink_to_external_source_are_outside_the_root(self) -> None:
        external = self.root / 'application-external'
        external.mkdir()
        target = external / 'target.py'
        target.write_text(self.target.read_text())
        alias = self.application / 'outside.py'
        alias.symlink_to(target)
        for path in (target, alias):
            with self.subTest(path=path):
                value = self.run_application('import _frozen_importlib_external as external\n'
                    f"loader=external.SourceFileLoader('target',{str(path)!r})\n"
                    "namespace={};exec(loader.get_code('target'),namespace);print(namespace['workload'](7))\n")
                self.assertTrue(value.capture_complete)
                self.assertEqual(len(value.roots), 1)

    def test_root_alias_resolves_to_the_same_application_directory(self) -> None:
        alias = self.root / 'root-alias'
        alias.symlink_to(self.application, target_is_directory=True)
        value = self.run_application('from target import workload\nprint(workload(7))\n', root=alias)
        self.assertTrue(value.capture_complete)
        self.assertEqual(len(value.roots), 2)

    def test_invalid_root_refuses_before_application_execution(self) -> None:
        self.entry.write_text("raise AssertionError('application must not start')\n")
        for root in (Path('.'), self.root / 'missing', self.target):
            with self.subTest(root=root):
                status, out, err, _, _ = self.launch(['-B', str(self.entry)], origin_root=root)
                self.assertEqual((status, out, err), (64, b'', b''))
