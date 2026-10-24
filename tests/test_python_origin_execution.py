"""Check the public summary's data contract; native authenticity is tested separately."""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path

from otelc import python_origin_execution as execution
from otelc.file_inputs import identity
from otelc.python_code_descriptor import encode
from test_python_origin_journal import (SOURCE, read_record, root_record,
    execution_record, sdk_record, join_record, wire)


class PythonOriginExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.entry = self.root / 'entry.py'
        self.entry.write_bytes(SOURCE)
        path = os.fsencode(self.entry)
        code = compile(SOURCE, str(self.entry), 'exec', dont_inherit=True)
        index = next(index for index, value in enumerate(code.co_consts) if type(value) is types.CodeType)
        child = code.co_consts[index]
        self.records = [read_record(path=path), root_record(path=path, content=encode(code)),
                        execution_record(path=path, edges=(index,)), sdk_record(path=path, code=child),
                        join_record(path=path)]
        self.journal = self.root / 'journal'

    def summary(self, records: list | None = None, *, plain: bool = False,
                final: dict | None = None, entry: dict | None = None, root: Path | None = None) -> dict:
        self.journal.write_bytes(wire(self.records if records is None else records, **(final or {})))
        self.journal.chmod(0o600)
        return execution.read(self.journal, expected_pid=12, entry=entry or identity(self.entry),
                              root=root or self.root, plain=plain)

    def test_complete_instrumented_and_baseline_data_keep_their_distinct_roles(self) -> None:
        instrumented = self.summary()
        self.assertEqual(instrumented['phase'], 'complete')
        self.assertEqual(instrumented['origins']['sdk_joins'], [[1, 30]])
        self.assertEqual(instrumented['origins']['root_ids'], [10])
        self.assertEqual(instrumented['codes'][0]['id'], 1)
        baseline = self.summary(self.records[:3], plain=True)
        self.assertEqual(baseline['phase'], 'complete')
        self.assertEqual((baseline['codes'], baseline['origins']['sdk_joins']), ([], []))

    def test_partial_capture_and_unbound_code_cannot_be_complete_summaries(self) -> None:
        for final in ({'lost': 1}, {'io_lost': 1}, {'finalized': 0}):
            with self.subTest(final=final):
                observed = self.summary(final=final)
                self.assertEqual(observed['phase'], 'failed')
                self.assertIn('native_capture', observed['origins']['failures'])
        self.assertIn('selected_sdk_origins', self.summary(self.records[:-1])['origins']['failures'])
        self.assertIn('baseline_sdk_observations', self.summary(plain=True)['origins']['failures'])

    def test_entry_and_scope_are_checked_against_separately_supplied_intent(self) -> None:
        expected = {**identity(self.entry), 'sha256': '0'*64}
        self.assertIn('entry_origin', self.summary(entry=expected)['origins']['failures'])
        self.assertIn('entry_origin', self.summary(final={'expected': 0})['origins']['failures'])
        self.assertIn('entry_origin', self.summary(self.records[:1])['origins']['failures'])
        self.assertIn('root_scope', self.summary(root=self.root / 'other')['origins']['failures'])
        self.journal.write_bytes(b'not an origin journal')
        self.journal.chmod(0o600)
        with self.assertRaises(ValueError):
            execution.read(self.journal, expected_pid=12, entry=identity(self.entry), root=self.root, plain=False)
