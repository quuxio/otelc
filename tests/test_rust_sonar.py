"""Reject incomplete Rust analysis evidence and preserve each Make coverage gate."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ci import rust_sonar
from otelc.file_inputs import identity

ROOT = Path(__file__).resolve().parents[1]


class RustAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / 'build'
        self.output.mkdir()
        for name in (*rust_sonar.RUST_SOURCES, 'native/rust/compiler/src/request_tests.rs',
                     'native/rust/bridge/src/keys_tests.rs',
                     'ci/rust_compiler_cases.py', 'ci/check_rust_compiler.py', 'ci/rust_sonar.py',
                     'otelc/health.py', 'tests/test_rust_drop_health.py', 'ci/rust_lifetime_cases.py',
                     'ci/rust_drop_cases.py',
                     'otelc/rust_lifetimes.py', 'tests/test_rust_lifetimes.py',
                     'ci/rust_module_cases.py', 'otelc/rust_modules.py', 'tests/test_rust_modules.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('// original source\n' * 10)
        for component in ('compiler', 'bridge'):
            for name in ('Cargo.toml', 'Cargo.lock'):
                (self.root / 'native/rust' / component / name).write_text('locked fixture\n')
        self.inputs = [identity(path) for path in rust_sonar.source_paths(self.root)]
        self.summaries = {str(self.root / name): {'count': 10, 'covered': 9, 'percent': 90}
                          for name in rust_sonar.RUST_SOURCES}
        self.lcov = ''.join('SF:' + str(self.root / name) + '\n'
            + ''.join(f'DA:{line},{int(line < 10)}\n' for line in range(1, 11))
            + 'LF:10\nLH:9\nend_of_record\n' for name in rust_sonar.RUST_SOURCES)
        self.clippy = b'{"reason":"build-finished","success":true}\n' * 2

    def write(self, **changes) -> None:
        values = dict(root=self.root, source_inputs=self.inputs, lcov=self.lcov,
                      summaries=self.summaries, clippy=self.clippy, output=self.output)
        values.update(changes)
        rust_sonar.write(**values)

    def test_complete_measured_catalogue_exports_relative_paths_and_rechecks_sources(self) -> None:
        self.write()
        rust_sonar.verify(self.root, self.output)
        text = (self.output / 'rust-coverage.lcov').read_text()
        self.assertEqual(text.count('SF:'), len(rust_sonar.RUST_SOURCES))
        self.assertNotIn(str(self.root), text)
        self.assertEqual((self.output / 'rust-clippy.json').read_bytes(), self.clippy)

    def test_new_production_source_cannot_silently_escape_the_coverage_gate(self) -> None:
        (self.root / 'native/rust/compiler/src/forgotten.rs').write_text('fn hidden() {}\n')
        with self.assertRaisesRegex(ValueError, 'catalogue changed'):
            rust_sonar.source_paths(self.root)

    def test_changed_source_missing_summary_and_incomplete_lcov_refuse(self) -> None:
        path = self.root / rust_sonar.RUST_SOURCES[0]
        path.write_text('changed source\n' * 10)
        with self.assertRaisesRegex(ValueError, 'current source bytes'):
            self.write()
        path.write_text('// original source\n' * 10)
        with self.assertRaisesRegex(ValueError, 'every production module'):
            self.write(summaries={})
        for text in (self.lcov + self.lcov, self.lcov.split('end_of_record\n', 1)[1]):
            with self.subTest(text=len(text)), self.assertRaises(ValueError):
                self.write(lcov=text)

    def test_low_physical_line_coverage_refuses_even_with_a_good_summary(self) -> None:
        with self.assertRaisesRegex(ValueError, 'below 80%'):
            self.write(lcov=self.lcov.replace('DA:1,1', 'DA:1,0').replace('DA:2,1', 'DA:2,0'))

    def test_empty_failed_or_warning_clippy_results_refuse(self) -> None:
        cases = (b'', b'not JSON', b'{}', self.clippy[:len(self.clippy) // 2],
                 self.clippy.replace(b'true', b'false', 1),
                 self.clippy + b'{"reason":"compiler-message","message":null}\n',
                 self.clippy + b'{"reason":"compiler-message","message":{"level":"warning"}}\n')
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                rust_sonar.clippy_report(raw)

    def test_report_replacement_and_dependency_drift_refuse_before_analysis(self) -> None:
        self.write()
        for name in ('rust-coverage.lcov', 'rust-clippy.json'):
            path = self.output / name
            original = path.read_bytes()
            path.write_bytes(original + b'\n')
            with self.subTest(report=name), self.assertRaisesRegex(ValueError, 'report changed'):
                rust_sonar.verify(self.root, self.output)
            path.write_bytes(original)
        (self.root / 'ci/rust_compiler_cases.py').write_text('changed case logic\n')
        with self.assertRaisesRegex(ValueError, 'source bindings changed'):
            rust_sonar.verify(self.root, self.output)

    def test_incomplete_wrongly_typed_or_rehashed_manifest_refuses(self) -> None:
        self.write()
        path = self.output / 'rust-analysis-inputs.json'
        original = json.loads(path.read_bytes())
        for field, replacement in (('schema_version', True), ('source_inputs', []),
                                   ('sources', []), ('reports', {})):
            value = original | {field: replacement}
            path.write_text(json.dumps(value))
            with self.subTest(field=field), self.assertRaises(ValueError):
                rust_sonar.verify(self.root, self.output)
        original['sources'][rust_sonar.RUST_SOURCES[0]]['sha256'] = '0' * 64
        path.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, 'source changed'):
            rust_sonar.verify(self.root, self.output)


class RustMakeGateTests(unittest.TestCase):
    def dry(self, target: str) -> str:
        return subprocess.check_output(['make', '-n', target, 'PYTHON=python3'], cwd=ROOT, text=True)

    def test_native_launch_keeps_both_existing_component_floors(self) -> None:
        text = self.dry('native-launch-check')
        self.assertIn("--include='*/otelc/native_launch.py' --fail-under=80", text)
        self.assertIn("--include='*/ci/native_launch_suite.py' --fail-under=80", text)
        self.assertNotIn('ci.check_rust_compiler', text)

    def test_rust_recipe_generates_and_checks_its_own_measured_inputs(self) -> None:
        text = self.dry('rust-compiler-check')
        self.assertIn('-m ci.check_rust_compiler', text)
        self.assertIn('-m ci.rust_sonar', text)
        self.assertIn('unittest discover -s tests -p test_rust_sonar.py', text)
        self.assertIn('build/rust-case.coverage', text)
        self.assertNotIn('native-launch-python.coverage', text)
        self.assertIn('test rust-compiler-check', (ROOT / 'Makefile').read_text())

    def test_lifetime_recipe_keeps_both_new_python_floors_and_no_sdk_build(self) -> None:
        text = self.dry('rust-lifetime-check')
        self.assertIn('-m ci.rust_lifetime_cases', text)
        for name in ('ci/rust_lifetime_cases.py', 'otelc/rust_lifetimes.py'):
            self.assertIn("--include='*/" + name + "' --fail-under=80", text)
        self.assertNotIn('build_sdk', text)
        workflow = (ROOT / '.github/workflows/rust-lifetimes.yml').read_text()
        self.assertIn('summaries(', workflow)
        self.assertIn('native/rust/compiler/src/lifetimes.rs', workflow)
        self.assertNotIn('make setup check', workflow)
