"""Exercise the real C framing writer, independently of origin observation."""

from __future__ import annotations

import subprocess

from python_input_support import NativeInputCase
from otelc import python_input_journal as inputs
from otelc import python_origin_journal as origins


class PythonOriginWriterTests(NativeInputCase):
    def test_concurrent_reads_have_distinct_sequences_and_retained_bytes(self) -> None:
        path = self.root / 'threads.journal'
        child = subprocess.Popen([str(self.fixture), str(path), 'event-threads'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(child.communicate(timeout=15), (b'', b''))
        self.assertEqual(child.returncode, 0)
        value = inputs.read(path, expected_pid=child.pid)
        self.assertEqual((value.capture_failures, value.journal_failures), (0, 0))
        self.assertEqual({item.content for item in value.imports},
                         {bytes((index, 255, 10)) for index in range(16)})

    def test_native_writer_uses_exact_read_sequences_version_and_per_kind_widths(self) -> None:
        path = self.root / 'origins.journal'
        child = subprocess.Popen([str(self.fixture), str(path), 'origins'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(child.communicate(timeout=15), (b'', b''))
        self.assertEqual(child.returncode, 0)
        records = list(origins._frames(path.read_bytes()))
        self.assertEqual([record[:2] for record in records], [(kind, sequence) for sequence, kind in
                         enumerate((1, 3, 5, 6, 7, 2), 1)])
        self.assertEqual(origins.ROOT.unpack_from(records[2][3]),
                         (child.pid, 10, 20, 0, 1, 1, 0x030c00f0, 2, 0, 0))
        self.assertEqual(records[2][3][origins.ROOT.size:], b'opaque')
        self.assertEqual(origins.EXECUTION.unpack_from(records[3][3]),
                         (child.pid, 30, 10, 0, 0x030c00f0, 1, 0))
        self.assertEqual(records[3][3][origins.EXECUTION.size:], b'\0' * 8)
        self.assertEqual(origins.JOIN.unpack(records[4][3]), (child.pid, 1, 30, 0, 0, 0, 0))
        self.assertEqual(inputs.FINAL.unpack(records[5][3]), (child.pid, 5, 20, 0, 0, 1, 1))
        # This fixture deliberately writes opaque graph bytes. Framing alone
        # cannot qualify it as a genuine compiler/VM origin relation.
        prepared_input = path.read_bytes()
        with self.assertRaises(ValueError):
            origins.decode(prepared_input, expected_pid=child.pid)

    def test_old_profile_refuses_origin_records_and_event_failures_reset_the_token(self) -> None:
        for mode in ('origin-v1', 'origin-invalid'):
            with self.subTest(mode=mode):
                path = self.root / (mode + '.journal')
                child = subprocess.Popen([str(self.fixture), str(path), mode],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(child.communicate(timeout=15), (b'', b''))
                self.assertEqual(child.returncode, 0)
                if mode == 'origin-v1':
                    value = inputs.read(path, expected_pid=child.pid)
                    self.assertFalse(value.complete)
                    self.assertEqual(value.capture_failures, 10)
                else:
                    records = list(origins._frames(path.read_bytes()))
                    self.assertEqual(inputs.FINAL.unpack(records[-1][3]),
                                     (child.pid, 3, 6, 9, 0, 1, 1))
