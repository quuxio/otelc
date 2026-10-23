"""Readiness paths cannot expose partially written canonical launch records."""

from __future__ import annotations

import os
import tempfile
import threading
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


    def test_in_progress_publication_reads_no_bytes_and_a_fresh_poll_reads_after_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ready.json'
            available = threading.Event()
            release = threading.Event()
            failures = []
            real_link = os.link

            def link(*args, **kwargs):
                real_link(*args, **kwargs)
                available.set()
                if not release.wait(3):
                    raise TimeoutError('consumer did not observe publication')

            def publish():
                try:
                    files.write(path, {'value': 'original'})
                except BaseException as error:
                    failures.append(error)

            with patch.object(io.os, 'link', side_effect=link):
                publisher = threading.Thread(target=publish)
                publisher.start()
                try:
                    self.assertTrue(available.wait(3))
                    with patch.object(files.os, 'fdopen') as read:
                        with self.assertRaises(files.PublicationPending):
                            files.read(path)
                    read.assert_not_called()
                finally:
                    release.set()
                    publisher.join(timeout=3)
            self.assertFalse(publisher.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertEqual(files.read(path)[0], {'value': 'original'})

    def test_final_publication_still_refuses_rewrite_after_initial_stat_before_data_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ready.json'
            files.write(path, {'value': 'original'})
            real_stat = os.fstat
            reads = 0

            def metadata(descriptor):
                nonlocal reads
                info = real_stat(descriptor)
                reads += 1
                if reads == 1:
                    path.write_bytes(recipe.encoded({'value': 'replaced'}).encode())
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                return info

            with patch.object(files.os, 'fstat', side_effect=metadata):
                with self.assertRaisesRegex(ValueError, 'changed while reading'):
                    files.read(path)

    def test_rewrite_before_first_data_read_during_publication_is_not_a_completed_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ready.json'
            staged = path.with_name(path.name + '.staging')
            staged.write_bytes(recipe.encoded({'value': 'original'}).encode())
            staged.chmod(0o600)
            os.link(staged, path)
            real_stat = os.fstat
            reads = 0

            def metadata(descriptor):
                nonlocal reads
                info = real_stat(descriptor)
                reads += 1
                if reads == 1:
                    self.assertEqual(info.st_nlink, 2)
                    path.write_bytes(recipe.encoded({'value': 'replaced'}).encode())
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                    staged.unlink()
                return info

            with patch.object(files.os, 'fstat', side_effect=metadata):
                with self.assertRaises(FileNotFoundError):
                    files.read(path)

    def test_ctime_change_without_publication_link_cleanup_still_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ready.json'
            files.write(path, {'value': 'original'})
            real_stat = os.fstat
            reads = 0

            def metadata(descriptor):
                nonlocal reads
                reads += 1
                if reads == 2:
                    os.chmod(path, 0o600)
                return real_stat(descriptor)

            with patch.object(files.os, 'fstat', side_effect=metadata):
                with self.assertRaisesRegex(ValueError, 'changed while reading'):
                    files.read(path)


    def test_other_link_counts_are_invalid_without_reading_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ready.json'
            files.write(path, {'value': 'original'})
            os.link(path, path.with_name('other-1'))
            os.link(path, path.with_name('other-2'))
            with patch.object(files.os, 'fdopen') as read:
                with self.assertRaisesRegex(ValueError, 'one final link'):
                    files.read(path)
            read.assert_not_called()
