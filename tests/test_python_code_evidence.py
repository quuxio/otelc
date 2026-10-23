"""Final journal comparison preserves consumed bytes and incomplete evidence."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from otelc.file_inputs import identity
from otelc.python_code_descriptor import encode
from otelc.python_code_evidence import inspect
from otelc import python_input_journal as inputs
from test_python_input_journal import final, record, source


class PythonCodeEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.entry = self.root / 'app.py'
        self.journal = self.root / 'inputs.journal'
        self.interpreter = identity(Path(sys.executable))

    def seal(self, content=b'def workload():\n    return 42\n', **end) -> None:
        self.entry.write_bytes(content)
        module = compile(content, str(self.entry), 'exec', dont_inherit=True)
        function = next(value for value in module.co_consts if type(value) is type(module))
        descriptor = encode(function)
        code = inputs.CODE.pack(12, 1, 0, sys.hexversion, 2, len(descriptor), 0) + descriptor
        self.journal.write_bytes(source(content=content, path=os.fsencode(self.entry))
            + record(4, 2, code, os.fsencode(self.entry))
            + final(count=2, size=len(content) + len(descriptor), sequence=3, **end))
        self.journal.chmod(0o600)

    def inspect(self, **options):
        return inspect(self.journal, expected_pid=12, interpreter=self.interpreter, **options)

    def test_changed_or_deleted_current_source_cannot_replace_consumed_bytes(self) -> None:
        self.seal()
        self.entry.write_text('raise RuntimeError("changed")\n')
        for removed in (False, True):
            if removed:
                self.entry.unlink()
            result = self.inspect()
            self.assertEqual(result['phase'], 'compared')
            self.assertEqual(result['codes'][0]['status'], 'unique-match')
            self.assertFalse(result['source_complete'])

    def test_failed_capture_finalization_and_missing_code_are_explicit(self) -> None:
        for end in ({'failures': 1}, {'io_failures': 1}, {'finalized': 0}):
            self.seal(**end)
            with mock.patch('otelc.python_code_evidence.build', side_effect=AssertionError('worker')):
                self.assertEqual(self.inspect()['reason'], 'capture-incomplete')
        self.journal.write_bytes(source() + final())
        self.assertEqual(self.inspect()['phase'], 'no-selected-code')

    def test_malformed_framing_and_actual_pid_mismatch_are_not_comparisons(self) -> None:
        self.seal()
        with self.assertRaises(ValueError):
            inspect(self.journal, expected_pid=13, interpreter=self.interpreter)
        self.journal.write_bytes(self.journal.read_bytes()[:-1])
        with self.assertRaises(ValueError):
            self.inspect()

    def test_deadline_and_failed_worker_do_not_become_complete(self) -> None:
        self.seal()
        with mock.patch('otelc.python_code_evidence.monotonic', side_effect=[0, 2]):
            self.assertEqual(self.inspect(timeout=1)['reason'], 'comparison-deadline')
        self.interpreter = {**self.interpreter, 'sha256': '0' * 64}
        result = self.inspect()
        self.assertEqual(result['phase'], 'incomplete')
        self.assertEqual(result['codes'][0]['status'], 'incomplete')
        for options in ({'timeout': True}, {'timeout': 31}, {'flags': True}, {'optimize': 3}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.inspect(**options)


if __name__ == '__main__':
    unittest.main()
