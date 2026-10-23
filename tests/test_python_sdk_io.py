"""Owned metadata and reply helpers retain complete bounded output."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from otelc import python_sdk_io as io
from otelc.python_sdk_process import private_json


class PythonSdkIoTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_publish_is_complete_private_exclusive_and_preserves_existing_data(self) -> None:
        path = self.root / 'ready.json'
        io.publish(path, {'schema_version': 1, 'name': 'α'})
        self.assertEqual(private_json(path), {'schema_version': 1, 'name': 'α'})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(path.with_name(path.name + '.staging').exists())
        with self.assertRaises(FileExistsError):
            io.publish(path, {'changed': True})
        self.assertEqual(private_json(path)['name'], 'α')
        self.assertFalse(path.with_name(path.name + '.staging').exists())

    def test_inherited_plan_requires_a_private_regular_file_and_closes_its_handle(self) -> None:
        path = self.root / 'plan'
        path.write_text('{"schema_version":1}')
        path.chmod(0o600)
        descriptor = os.open(path, os.O_RDONLY)
        self.assertEqual(io.inherited_plan(descriptor), {'schema_version': 1})
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        path.chmod(0o644)
        descriptor = os.open(path, os.O_RDONLY)
        with self.assertRaises(ValueError):
            io.inherited_plan(descriptor)
        path.chmod(0o600)
        with path.open('wb') as output:
            output.truncate(1024 * 1024 + 1)
        descriptor = os.open(path, os.O_RDONLY)
        with self.assertRaises(ValueError):
            io.inherited_plan(descriptor)

    def test_nonobjects_duplicate_fields_nonfinite_and_public_reports_are_refused(self) -> None:
        for data in (b'[]', b'{"x":1,"x":2}', b'{"value":NaN}', b'{"value":1e999}'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                io.decode(data)
        path = self.root / 'report.json'
        path.write_text('{}')
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            private_json(path)
        path.chmod(0o600)
        with path.open('wb') as output:
            output.truncate(io.MAX_REPLY + 1)
        with self.assertRaises(ValueError):
            private_json(path)
        link = self.root / 'linked.json'
        link.symlink_to(path)
        with self.assertRaises(OSError):
            private_json(link)

    def test_reply_framing_handles_real_backpressure_and_disconnected_callers(self) -> None:
        reader, descriptor = os.pipe()
        os.set_blocking(descriptor, False)
        try:
            self.assertTrue(io.reply(descriptor, {'id': 1, 'ok': True}))
            self.assertEqual(io.decode(os.read(reader, 4096)), {'id': 1, 'ok': True})
            while True:
                try:
                    os.write(descriptor, b'x' * 512)
                except BlockingIOError:
                    break
            self.assertFalse(io.reply(descriptor, {'id': 2}, timeout=.005))
            self.assertFalse(io.reply(descriptor, {'data': 'x' * io.MAX_REPLY}))
        finally:
            os.close(reader)
        try:
            self.assertFalse(io.reply(descriptor, {'id': 3}))
        finally:
            os.close(descriptor)
