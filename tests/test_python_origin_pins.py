"""Original loader function AND code identities precede application startup."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

from ci import build_python_inputs as tooling
from otelc import python_input_journal as journal
from python_input_support import NativeInputCase


class PythonOriginPinsTests(NativeInputCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        root = Path(__file__).resolve().parents[1]
        cls.pins_fixture = root / 'build/native' / ('python-origin-pins-fixture-' + sys.implementation.cache_tag)
        _, flags = tooling.toolchain()
        library = 'libotelc_python_inputs' + ('.dylib' if sys.platform == 'darwin' else '.so')
        coverage = ['-fprofile-instr-generate', '-fcoverage-mapping'] if os.environ.get('OTELC_NATIVE_COVERAGE') else []
        with tempfile.TemporaryDirectory(dir=root / 'build/native') as temporary:
            candidate = Path(temporary) / 'pins'
            subprocess.run([tooling.compiler(), str(root / 'tests/fixtures/python_origin_pins.c'),
                str(cls.tool.parent / library), '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-I' + str(root / 'native/python'), '-Wl,-rpath,' + str(cls.tool.parent),
                '-o', str(candidate), *coverage, *flags], check=True)
            os.replace(candidate, cls.pins_fixture)

    def test_function_and_code_changes_missing_helpers_and_ownership_refuse(self) -> None:
        for mode in ('ordinary', 'late', 'fork', 'interpreter', 'module', 'class', 'function',
                     'compiler', 'replace', 'code0', 'code1', 'code2', 'code3'):
            with self.subTest(mode=mode):
                result = subprocess.run([str(self.pins_fixture), mode, sys.executable], capture_output=True, timeout=15)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))

    def test_original_channels_and_main_code_survive_normal_isolated_and_no_site(self) -> None:
        entry = self.root / 'original.py'
        entry.write_text('import sys\nsys.stdout.buffer.write(b"out\\x00\\xff")\nsys.stderr.buffer.write(b"err\\x00\\xfe")\n')
        for flags in ([], ['-I'], ['-S'], ['-I', '-S']):
            with self.subTest(flags=flags):
                plain = subprocess.run([sys.executable, *flags, '-B', str(entry)], capture_output=True, timeout=15)
                status, out, err, path, pid = self.launch([*flags, '-B', str(entry)], origin_observer=True)
                self.assertEqual((status, out, err), (plain.returncode, plain.stdout, plain.stderr))
                self.assertTrue(journal.read(path, expected_pid=pid).complete)

    def test_startup_changes_are_detected_without_replacing_application_behaviour(self) -> None:
        entry = self.root / 'original.py'
        entry.write_text('print("original app")\n')
        hook = self.root / 'sitecustomize.py'
        environment = {**os.environ, 'PYTHONPATH': str(self.root)}
        for change in ('loader.source_to_code = lambda *args, **kwargs: None',
                       'loader.source_to_code.__code__ = (lambda *args, **kwargs: None).__code__'):
            hook.write_text('from _frozen_importlib_external import SourceLoader as loader\n' + change + '\nprint("startup hook")\n')
            with self.subTest(change=change):
                plain = subprocess.run([sys.executable, '-B', str(entry)], env=environment, capture_output=True, timeout=15)
                status, out, err, path, pid = self.launch(['-B', str(entry)], origin_observer=True, environment=environment)
                self.assertEqual((status, out, err), (plain.returncode, plain.stdout, plain.stderr))
                value = journal.read(path, expected_pid=pid)
                self.assertGreater(value.capture_failures, 0)
                self.assertFalse(value.complete)

    def test_normal_profile_does_not_claim_origin_guard_health(self) -> None:
        entry = self.root / 'original.py'
        entry.write_text('print("ordinary")\n')
        (self.root / 'sitecustomize.py').write_text('from _frozen_importlib_external import SourceLoader\nSourceLoader.source_to_code = lambda *args, **kwargs: None\n')
        status, out, err, path, pid = self.launch(['-B', str(entry)], environment={**os.environ, 'PYTHONPATH': str(self.root)})
        self.assertEqual((status, out, err), (0, b'ordinary\n', b''))
        self.assertTrue(journal.read(path, expected_pid=pid).complete)
