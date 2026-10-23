"""Native source receipts reject partial, altered and unsupported evidence."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otelc import python_input_journal as journal


def record(kind: int, sequence: int, payload: bytes, path: bytes = b'') -> bytes:
    return journal.HEADER.pack(journal.MAGIC, kind, 0, 0, sequence, len(path), len(payload)) + path + payload


def source(*, pid: int = 12, content: bytes = b'print(7)\n', path: bytes = b'entry.py',
           nanos: int = 3, mode: int = stat.S_IFREG | 0o644) -> bytes:
    metadata = journal.SOURCE.pack(pid, 1, 2, len(content), -1, nanos, mode)
    return record(1, 1, metadata + content, path)


def final(*, pid: int = 12, count: int = 1, size: int = 9, failures: int = 0,
          io_failures: int = 0, finalized: int = 1, expected: int = 1,
          sequence: int = 2) -> bytes:
    return record(2, sequence, journal.FINAL.pack(pid, count, size, failures, io_failures, finalized, expected))


class PythonInputJournalTests(unittest.TestCase):
    def test_exact_source_identity_bytes_signed_timestamp_and_final_counters(self) -> None:
        value = journal.decode(source() + final(), expected_pid=12)
        self.assertTrue(value.complete)
        self.assertEqual((value.pid, value.sources[0].path, value.sources[0].content),
                         (12, b'entry.py', b'print(7)\n'))
        self.assertEqual(value.sources[0].mtime_ns, -999999997)
        self.assertEqual(value.sources[0].sha256,
                         '896abce6abd56a56397a878660d2cba6f1398ef128ca6ead0acac98a6c8f38f7')

    def test_failures_absent_entry_and_unfinalized_reports_are_not_complete(self) -> None:
        for change in ({'failures': 1}, {'io_failures': 1}, {'finalized': 0}, {'expected': 0}):
            with self.subTest(change=change):
                self.assertFalse(journal.decode(source() + final(**change), expected_pid=12).complete)
        value = journal.decode(final(count=0, size=0, sequence=1), expected_pid=12)
        self.assertFalse(value.complete)

    def test_partial_out_of_order_unknown_and_trailing_records_are_refused(self) -> None:
        good = source() + final()
        cases = (b'', good[:1], source(), good[:-1], good + b'x', final(),
                 record(9, 1, b'x' * 56), good.replace(journal.MAGIC, b'WRONG123', 1),
                 record(1, 2, journal.SOURCE.pack(12, 1, 2, 0, 0, 0, stat.S_IFREG), b'entry.py'),
                 record(2, 1, b'x' * 57), record(2, 1, journal.FINAL.pack(12, 0, 0, 0, 0, 1, 1), b'path'))
        for data in cases:
            with self.subTest(data=data[:40]), self.assertRaises(ValueError):
                journal.decode(data, expected_pid=12)
        with mock.patch.object(journal, 'MAX_JOURNAL', len(good) - 1):
            with self.assertRaises(ValueError):
                journal.decode(good, expected_pid=12)
        for pid in (True, 0, -1):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                journal.decode(good, expected_pid=pid)

    def test_metadata_counts_and_actual_process_identity_must_match(self) -> None:
        cases = [source(pid=13) + final(), source(path=b'') + final(),
                 source(path=b'x\0y') + final(), source(nanos=10 ** 9) + final(),
                 source(mode=stat.S_IFDIR) + final()]
        cases.extend(source() + final(**change) for change in (
            {'pid': 13}, {'count': 2}, {'size': 8}, {'finalized': 2}, {'expected': 2}))
        for data in cases:
            with self.subTest(data=data[:40]), self.assertRaises(ValueError):
                journal.decode(data, expected_pid=12)

    def test_reader_requires_private_regular_bounded_nonfollowing_input(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            root = Path(temporary)
            path = root / 'inputs.journal'
            path.write_bytes(source() + final())
            path.chmod(0o600)
            self.assertTrue(journal.read(path, expected_pid=12).complete)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                journal.read(path, expected_pid=12)
            path.chmod(0o600)
            link = root / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                journal.read(link, expected_pid=12)
            fifo = root / 'fifo'
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ValueError):
                journal.read(fifo, expected_pid=12)
            with path.open('wb') as stream:
                stream.truncate(journal.MAX_JOURNAL + 1)
            with self.assertRaises(ValueError):
                journal.read(path, expected_pid=12)

    def test_reader_refuses_actual_mid_read_change_and_closes_failed_wrappers(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            path = Path(temporary) / 'inputs.journal'
            path.write_bytes(source() + final())
            path.chmod(0o600)
            before = len(os.listdir('/dev/fd'))
            original = os.fdopen

            @contextlib.contextmanager
            def changing_stream(*args, **kwargs):
                with original(*args, **kwargs) as stream:
                    class Reader:
                        def read(self, size):
                            data = stream.read(size)
                            path.write_bytes(data + b'changed')
                            return data
                    yield Reader()

            with mock.patch.object(journal.os, 'fdopen', side_effect=changing_stream):
                with self.assertRaisesRegex(ValueError, 'changed while reading'):
                    journal.read(path, expected_pid=12)
            self.assertEqual(len(os.listdir('/dev/fd')), before)
            with mock.patch.object(journal.os, 'fdopen', side_effect=OSError('wrapper failed')):
                with self.assertRaisesRegex(OSError, 'wrapper failed'):
                    journal.read(path, expected_pid=12)
            self.assertEqual(len(os.listdir('/dev/fd')), before)
