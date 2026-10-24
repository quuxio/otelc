"""The native floor uses actual execution and refuses missing or weak evidence."""

from __future__ import annotations

import copy
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ci import check_python_inputs_coverage as gate


class PythonInputCoverageTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'universal Mach-O coverage is macOS-specific')
    def test_universal_and_thin_objects_select_the_executed_slice(self) -> None:
        llvm = Path(gate.tooling.compiler()).parent
        architecture = platform.machine()
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory).resolve()
            source = root / 'fixture.c'
            source.write_text('int main(int argc, char **argv) { (void)argv; return argc > 1 ? 0 : 1; }\n')
            fat, thin = root / 'universal', root / 'thin'
            flags = ['-fprofile-instr-generate', '-fcoverage-mapping']
            for output, targets in ((fat, ['-arch', 'arm64', '-arch', 'x86_64']), (thin, [])):
                subprocess.run([str(llvm / 'clang'), *flags, *targets, str(source), '-o', str(output)],
                               check=True, capture_output=True)
            raw, merged = root / 'fixture.profraw', root / 'fixture.profdata'
            subprocess.run([str(fat), 'covered'], env={**os.environ, 'LLVM_PROFILE_FILE': str(raw)}, check=True)
            subprocess.run([str(llvm / 'llvm-profdata'), 'merge', str(raw), '-o', str(merged)],
                           check=True, capture_output=True)
            command = gate.export_command(llvm, [fat, thin], merged, architecture)
            without_architecture = [part for part in command if not part.startswith('-arch=')]
            before = subprocess.run(without_architecture, capture_output=True, text=True)
            self.assertNotEqual(before.returncode, 0)
            self.assertIn('specifier is invalid or missing for universal binary', before.stderr)
            self.assertEqual(command.count('-arch=' + architecture), 2)
            data = json.loads(subprocess.check_output(command, text=True))
            files = [row for entry in data['data'] for row in entry['files']
                     if Path(row['filename']).resolve() == source]
            self.assertEqual(len(files), 1)
            self.assertGreater(files[0]['summary']['lines']['covered'], 0)

    def test_real_native_profiles_cover_each_component_and_retain_branch_counts(self) -> None:
        result = gate.check()
        self.assertEqual(result['version'], sys.version)
        self.assertEqual(set(result['sources']), set(gate.NATIVE_SOURCES))
        for name, summary in result['sources'].items():
            self.assertGreater(summary['lines']['count'], 0)
            self.assertGreaterEqual(summary['lines']['percent'], 80)
            if name not in ('OriginChain.c', 'OriginEntry.c', 'OriginLoader.c') or result['origin_chain_available']:
                self.assertGreater(summary['branches']['count'], 0)
            else:
                self.assertEqual(summary['branches']['count'], 0)

    def test_low_empty_missing_or_duplicate_actual_sources_cannot_pass(self) -> None:
        rows = [{'filename': str(gate.ROOT / 'native/python' / name),
                 'summary': {'lines': {'count': 1, 'percent': 100}}} for name in gate.NATIVE_SOURCES]
        self.assertEqual(set(gate.summaries({'data': [{'files': rows}]})), set(gate.NATIVE_SOURCES))
        for change in ({'count': 0, 'percent': 100}, {'count': 10, 'percent': 79}):
            altered = copy.deepcopy(rows)
            altered[0]['summary']['lines'] = change
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'below80'):
                gate.summaries({'data': [{'files': altered}]})
        for altered in (rows[1:], rows + [rows[0]]):
            with self.subTest(size=len(altered)), self.assertRaisesRegex(ValueError, 'identify'):
                gate.summaries({'data': [{'files': altered}]})
