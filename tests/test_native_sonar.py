"""Native analysis imports exact build entries and honest per-source line data."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from ci import native_sonar as sonar
from otelc import llvm_backend


class NativeSonarTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / 'build'
        self.output.mkdir()
        for relative in (*sonar.PRODUCTION, *sonar.TESTS):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('ordinary source\n' * 6)
        self.sources = {self.root / relative for relative in (*sonar.PRODUCTION, *sonar.TESTS)}
        header = self.root / 'native/llvm/function_abi.h'
        header.write_text('fixed ABI header\n')
        self.records = []
        for name, relatives in (('llvm-pass', sonar.PRODUCTION[:1]), ('native-control', sonar.PRODUCTION[1:3]),
                                ('native-launch', sonar.PRODUCTION[3:])):
            lcov = self.output / (name + '.lcov')
            lcov.write_text(''.join(self.lcov(self.root / relative) for relative in relatives))
            database = self.output / (name + '-compile.json')
            database.write_text(json.dumps([self.entry(self.root / relative) for relative in relatives]))
            self.records.append({'lcov': llvm_backend.identity(lcov),
                                 'compilation_database': llvm_backend.identity(database)})
        self.llvm = self.records[0] | {'source': llvm_backend.identity(self.root / sonar.PRODUCTION[0]),
                                     'headers': [llvm_backend.identity(header)],
                                     'line_coverage': {'count': 5, 'covered': 4}}
        self.native = self.records[1] | {
            'source_inputs': [llvm_backend.identity(self.root / relative) for relative in sonar.PRODUCTION[1:3]],
            'line_coverage': {str(self.root / relative): {'count': 5, 'covered': 4} for relative in sonar.PRODUCTION[1:3]}}
        self.launch = self.records[2] | {
            'source_inputs': [llvm_backend.identity(self.root / relative) for relative in sonar.PRODUCTION[3:]],
            'line_coverage': {str(self.root / relative): {'count': 5, 'covered': 4} for relative in sonar.PRODUCTION[3:]}}
        database = Path(self.native['compilation_database']['path'])
        database.write_text(json.dumps([self.entry(self.root / relative) for relative in (*sonar.PRODUCTION[1:3], *sonar.TESTS)]))
        self.native['compilation_database'] = llvm_backend.identity(database)
        self.write_records()

    def lcov(self, source: Path) -> str:
        return 'TN:\nSF:' + str(source) + '\nDA:1,1\nDA:2,2\nDA:3,1\nDA:4,1\nDA:5,0\nLF:5\nLH:4\nend_of_record\n'

    def entry(self, source: Path) -> dict:
        return {'directory': str(self.root), 'file': str(source),
                'arguments': ['/compiler with spaces/clang++', '-c', str(source)]}

    def write_records(self) -> None:
        for name, value in (('llvm-pass-source-coverage-identity.json', self.llvm),
                            ('native-control-coverage.json', self.native),
                            ('native-launch-coverage.json', self.launch)):
            (self.output / name).write_text(json.dumps(value))

    def test_lcov_keeps_uncovered_lines_and_refuses_incomplete_foreign_or_inconsistent_data(self) -> None:
        source = self.root / sonar.PRODUCTION[0]
        text = self.lcov(source)
        self.assertEqual(sonar.lcov_lines(text, source), {1: True, 2: True, 3: True, 4: True, 5: False})
        for changed in (text.replace(str(source), '/foreign.cpp'), text.replace('end_of_record', ''),
                        text.replace('LF:5', 'LF:6'), text.replace('LH:4', 'LH:5'),
                        text.replace('DA:2,2', 'DA:1,2'), text.replace('DA:1,1', 'DA:0,1'),
                        text.replace('DA:1,1', 'DA:1,-1'), text.replace('DA:1,1', 'DA:7,1'),
                        text.replace('DA:1,1', 'DA:1,1,extra,field'), text + 'LF:5\n',
                        text.replace('DA:1,1', 'DA:1,0').replace('LH:4', 'LH:3')):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                sonar.lcov_lines(changed, source)

    def test_compilation_entries_preserve_actual_argv_or_cmake_command_without_execution(self) -> None:
        records = [self.entry(path) for path in sorted(self.sources)]
        records[0].pop('arguments')
        records[0]['command'] = '/compiler -I"/headers with spaces" -c input.cpp'
        records.append(self.entry(self.root / 'upstream.cpp'))
        path = self.output / 'database.json'
        path.write_text(json.dumps(records))
        with patch('subprocess.run', side_effect=AssertionError('must not execute metadata')):
            selected = sonar.compilation_database([path], self.sources)
        self.assertEqual(selected, records[:len(self.sources)])

    def test_multi_source_lcov_cannot_drop_add_duplicate_or_truncate_a_record(self) -> None:
        expected = {self.root / relative for relative in sonar.PRODUCTION[1:]}
        text = ''.join(self.lcov(path) for path in sorted(expected))
        records = sonar.lcov_records(text, expected)
        self.assertEqual(set(records), expected)
        for source, record in records.items():
            self.assertEqual(sonar.lcov_lines(record, source), {1: True, 2: True, 3: True, 4: True, 5: False})
        for changed in (text + self.lcov(next(iter(expected))), text + self.lcov(self.root / 'foreign.cpp'),
                        self.lcov(next(iter(expected))), text.rsplit('end_of_record', 1)[0],
                        text.replace('SF:', 'SF:/foreign\nSF:', 1)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                sonar.lcov_records(changed, expected)

    def test_llvm_expansion_totals_are_checked_separately_from_physical_source_lines(self) -> None:
        source = self.root / sonar.PRODUCTION[0]
        text = self.lcov(source).replace('LF:5', 'LF:7').replace('LH:4', 'LH:6')
        summary = {'count': 7, 'covered': 6}
        lines = sonar.lcov_lines(text, source, summary=summary)
        self.assertEqual((len(lines), sum(lines.values())), (5, 4))
        for changed in ({'count': 8, 'covered': 6}, {'count': 7, 'covered': 5}):
            with self.subTest(summary=changed), self.assertRaises(ValueError):
                sonar.lcov_lines(text, source, summary=changed)
        with self.assertRaisesRegex(ValueError, 'below 80'):
            sonar.lcov_lines(text.replace('LH:6', 'LH:4'), source, summary={'count': 7, 'covered': 4})

    def test_compilation_database_refuses_missing_duplicate_or_unusable_context(self) -> None:
        records = [self.entry(path) for path in sorted(self.sources)]
        path = self.output / 'database.json'
        invalid = [{}, [None], [{'file': 4}], records[:2], records + [records[0]]]
        for key, value in (('directory', '/no-directory'), ('arguments', []), ('arguments', [4]),
                           ('arguments', ['compiler\0']), ('command', 'also-present')):
            changed = copy.deepcopy(records)
            changed[0][key] = value
            invalid.append(changed)
        for changed in invalid:
            path.write_text(json.dumps(changed))
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                sonar.compilation_database([path], self.sources)

    def test_checked_generation_has_exact_sources_and_keeps_database_only_for_analysis(self) -> None:
        with patch.dict(os.environ, {'OTELC_CPP_ANALYSIS': ''}):
            sonar.generate(self.root)
        self.assertFalse((self.output / 'native-compile-commands.json').exists())
        report = ET.parse(self.output / 'native-sonar-coverage.xml').getroot()
        self.assertEqual((report.tag, report.attrib), ('coverage', {'version': '1'}))
        self.assertEqual([file.attrib['path'] for file in report], list(sonar.PRODUCTION))
        for file in report:
            self.assertEqual(len(file), 5)
            self.assertEqual(file[-1].attrib, {'lineNumber': '5', 'covered': 'false'})
        with patch.dict(os.environ, {'OTELC_CPP_ANALYSIS': '1'}):
            sonar.generate(self.root)
        database = json.loads((self.output / 'native-compile-commands.json').read_bytes())
        self.assertEqual({Path(row['file']) for row in database}, self.sources)

    def test_changed_lcov_source_context_or_missing_implementation_input_refuse(self) -> None:
        for path in (Path(self.llvm['lcov']['path']), Path(self.native['compilation_database']['path']),
                     Path(self.launch['lcov']['path']), Path(self.launch['compilation_database']['path']),
                     self.root / sonar.PRODUCTION[0], self.root / sonar.PRODUCTION[1], self.root / sonar.PRODUCTION[3]):
            before = path.read_bytes()
            try:
                path.write_bytes(before + b'changed')
                with self.subTest(path=path), self.assertRaises(ValueError):
                    sonar.generate(self.root)
            finally:
                path.write_bytes(before)
        self.native['source_inputs'] = [llvm_backend.identity(self.root / sonar.TEST)]
        self.write_records()
        with self.assertRaisesRegex(ValueError, 'omitted'):
            sonar.generate(self.root)


if __name__ == '__main__':
    unittest.main()
