"""Readiness paths cannot expose partially written canonical launch records."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from otelc import python_launch_files as files
from otelc import python_recipe as recipe
from otelc import python_sdk_io as io


class LaunchPublicationTests(unittest.TestCase):
    def test_destination_is_absent_when_the_owned_file_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ready.json'
            create = io.create_file

            def observe(selected: Path) -> int:
                descriptor = create(selected)
                if path.exists():
                    os.close(descriptor)
                    self.fail('readiness path was visible before its complete write')
                return descriptor

            with patch.object(io, 'create_file', side_effect=observe):
                files.write(path, {'ready': True})
            self.assertEqual(path.read_bytes(), recipe.encoded({'ready': True}).encode())
            self.assertFalse(path.with_name(path.name + '.staging').exists())

    def test_partial_native_writes_are_finished_before_atomic_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ready.json'
            write = os.write

            def short(descriptor: int, data: bytes) -> int:
                self.assertFalse(path.exists())
                return write(descriptor, data[:3])

            with patch.object(io.os, 'write', side_effect=short) as observed:
                files.write(path, {'value': 'complete'})
            self.assertGreater(observed.call_count, 1)
            self.assertEqual(files.read(path)[0], {'value': 'complete'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_write_exposes_no_ready_file_and_preserves_existing_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ready.json'
            with patch.object(io, 'write_bytes', side_effect=OSError('failed write')):
                with self.assertRaises(OSError):
                    files.write(path, {'value': 'not ready'})
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name(path.name + '.staging').exists())
            files.write(path, {'value': 'preserved'})
            with self.assertRaises(FileExistsError):
                files.write(path, {'value': 'replacement'})
            self.assertEqual(files.read(path)[0], {'value': 'preserved'})
            self.assertFalse(path.with_name(path.name + '.staging').exists())
