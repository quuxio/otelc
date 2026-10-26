"""Regression tests for compressed qualification storage."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qualification_record as record


class QualificationRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "empty").mkdir()
        (self.source / "nested").mkdir()
        self.original = self.source / "nested/raw.json"
        self.original.write_bytes(b'{"observations":12345}\n' * 10000)
        self.original.chmod(0o640)
        os.utime(self.original, ns=(1234567890123456789,) * 2)
        self.archive = self.root / "record.zip"

    def pack(self) -> dict:
        return record.pack(self.source, self.archive)

    def rewrite(self, change) -> None:
        """Build altered fixtures with a matching external digest."""
        with zipfile.ZipFile(self.archive) as packed:
            contents = {name: packed.read(name) for name in packed.namelist()}
        change(contents)
        with zipfile.ZipFile(self.archive, "w") as packed:
            for name, data in contents.items():
                packed.writestr(name, data)
        receipt = json.loads(record.receipt_path(self.archive).read_text())
        receipt["archive_sha256"] = record.digest(self.archive)
        record.receipt_path(self.archive).write_text(json.dumps(receipt))

    def change_manifest(self, callback) -> None:
        def change(contents):
            manifest = json.loads(contents[record.MANIFEST])
            callback(manifest)
            contents[record.MANIFEST] = json.dumps(manifest).encode()
        self.rewrite(change)

    def test_round_trip_preserves_bytes_modes_times_empty_directories(self) -> None:
        before = record.snapshot(self.source)
        receipt = self.pack()
        self.assertLess(receipt["compressed_bytes"], receipt["original_bytes"] / 10)
        self.assertLess(record.receipt_path(self.archive).stat().st_size, 1024)
        destination = self.root / "restored"
        record.restore(self.archive, destination)
        self.assertEqual((destination / "nested/raw.json").read_bytes(),
                         self.original.read_bytes())
        for name, info in before.items():
            restored = (destination / name).stat()
            self.assertEqual(stat.S_IMODE(restored.st_mode), stat.S_IMODE(info.st_mode))
            self.assertEqual(restored.st_mtime_ns, info.st_mtime_ns)
        self.assertTrue((destination / "empty").is_dir())
        self.assertEqual(record.snapshot(self.source), before)

    def test_empty_files_binary_and_unicode_names(self) -> None:
        (self.source / "zero").touch()
        (self.source / "caf\u00e9").write_bytes(bytes(range(256)))
        self.pack()
        destination = self.root / "restored"
        record.restore(self.archive, destination)
        self.assertEqual((destination / "zero").read_bytes(), b"")
        self.assertEqual((destination / "caf\u00e9").read_bytes(), bytes(range(256)))

    def test_existing_archive_or_receipt_is_never_overwritten(self) -> None:
        self.pack()
        original = self.archive.read_bytes()
        with self.assertRaises(FileExistsError):
            self.pack()
        self.assertEqual(self.archive.read_bytes(), original)
        record.receipt_path(self.archive).unlink()
        with self.assertRaises(FileExistsError):
            self.pack()

    def test_archive_inside_source_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside"):
            record.pack(self.source, self.source / "bad.zip")

    def test_link_and_special_inputs_refused(self) -> None:
        link = self.source / "link"
        link.symlink_to(self.original)
        with self.assertRaisesRegex(ValueError, "regular"):
            self.pack()
        link.unlink()
        os.mkfifo(self.source / "fifo")
        with self.assertRaisesRegex(ValueError, "regular"):
            self.pack()

    def test_file_or_symlink_root_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "directory"):
            record.pack(self.original, self.archive)
        link = self.root / "link"
        link.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, "regular"):
            record.pack(link, self.archive)

    def test_unreadable_subtree_is_not_silently_omitted(self) -> None:
        def unreadable(*args, **kwargs):
            kwargs["onerror"](PermissionError("unreadable subtree"))
        with mock.patch.object(record.os, "walk", side_effect=unreadable):
            with self.assertRaisesRegex(PermissionError, "unreadable"):
                self.pack()
        self.assertFalse(self.archive.exists())

    def test_source_change_during_read_prevents_receipt(self) -> None:
        original_identity = record.identity
        with mock.patch.object(record, "identity", side_effect=lambda info:
                               (*original_identity(info), id(info))):
            with self.assertRaisesRegex(ValueError, "Source changed"):
                self.pack()
        self.assertFalse(record.receipt_path(self.archive).exists())
        self.assertTrue(self.original.exists())

    def test_new_file_during_compression_prevents_receipt(self) -> None:
        snapshot = record.snapshot
        calls = 0
        def changing_snapshot(root):
            nonlocal calls
            calls += 1
            if calls == 2:
                (root / "late").touch()
            return snapshot(root)
        with mock.patch.object(record, "snapshot", side_effect=changing_snapshot):
            with self.assertRaisesRegex(ValueError, "Source changed"):
                self.pack()
        self.assertFalse(record.receipt_path(self.archive).exists())

    def test_corrupt_archive_refuses_restore_before_creating_output(self) -> None:
        self.pack()
        with self.archive.open("ab") as stream:
            stream.write(b"corruption")
        destination = self.root / "restore"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            record.restore(self.archive, destination)
        self.assertFalse(destination.exists())

    def test_altered_payload_and_wrong_size_refused(self) -> None:
        self.pack()
        self.rewrite(lambda items: items.__setitem__("data/nested/raw.json", b"bad"))
        with self.assertRaisesRegex(ValueError, "Payload mismatch"):
            record.verify(self.archive)

    def test_extra_members_refused(self) -> None:
        self.pack()
        self.rewrite(lambda items: items.__setitem__("extra", b"bad"))
        with self.assertRaisesRegex(ValueError, "members"):
            record.verify(self.archive)

    def test_duplicate_members_refused(self) -> None:
        self.pack()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.archive, "a") as packed:
                packed.writestr("data/nested/raw.json", self.original.read_bytes())
        receipt = json.loads(record.receipt_path(self.archive).read_text())
        receipt["archive_sha256"] = record.digest(self.archive)
        record.receipt_path(self.archive).write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "members"):
            record.verify(self.archive)

    def test_unsafe_manifest_names_refused(self) -> None:
        self.pack()
        self.change_manifest(lambda value: value["entries"].__setitem__(
            "../escape", {"directory": True}))
        with self.assertRaisesRegex(ValueError, "path"):
            record.verify(self.archive)
        for name in ("/absolute", "a\\b", "./x", "a//b", "a/../b", ""):
            self.assertFalse(record.valid_name(name))

    def test_invalid_format_and_root_metadata_refused(self) -> None:
        self.pack()
        self.change_manifest(lambda value: value.__setitem__("format", 2))
        with self.assertRaisesRegex(ValueError, "format"):
            record.verify(self.archive)
        self.change_manifest(lambda value: value.__setitem__("format", 1))
        self.change_manifest(lambda value: value["entries"][""].__setitem__(
            "directory", False))
        with self.assertRaisesRegex(ValueError, "root"):
            record.verify(self.archive)

    def test_missing_root_and_receipt_count_refused(self) -> None:
        self.pack()
        self.change_manifest(lambda value: value["entries"].pop(""))
        with self.assertRaisesRegex(ValueError, "root"):
            record.verify(self.archive)

    def test_bad_receipt_count_refused(self) -> None:
        self.pack()
        path = record.receipt_path(self.archive)
        receipt = json.loads(path.read_text())
        receipt["entries"] += 1
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "count"):
            record.verify(self.archive)

    def test_restore_refuses_existing_directory_and_symlink(self) -> None:
        self.pack()
        with self.assertRaises(FileExistsError):
            record.restore(self.archive, self.source)
        link = self.root / "existing-link"
        link.symlink_to(self.source)
        with self.assertRaises(FileExistsError):
            record.restore(self.archive, link)

    def test_failed_command_is_archived_and_exit_status_preserved(self) -> None:
        code = record.run(self.archive, [sys.executable, "-c",
                          "import sys; print('failed'); sys.exit(7)"])
        self.assertEqual(code, 7)
        record.restore(self.archive, self.root / "failure")
        self.assertEqual((self.root / "failure/stdout.log").read_text(), "failed\n")
        self.assertEqual(list(self.root.glob(".qualification-*")), [])

    def test_successful_command_writes_evidence_to_injected_directory(self) -> None:
        code = record.run(self.archive, [sys.executable, "-c", "import os; "
                          "from pathlib import Path; "
                          "Path(os.environ['OTELC_QUALIFICATION_DIR'], 'data').write_text('ok')"])
        self.assertEqual(code, 0)
        manifest = record.verify(self.archive)
        self.assertIn("data", manifest["entries"])

    def test_missing_command_and_empty_command(self) -> None:
        with self.assertRaises(ValueError):
            record.run(self.archive, [])
        self.assertEqual(record.run(self.archive, [str(self.root / "missing")]), 127)

    def test_compression_failure_keeps_raw_run_for_recovery(self) -> None:
        with mock.patch.object(record, "pack", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                record.run(self.archive, [sys.executable, "-c", "print('retain')"])
        stage, = self.root.glob(".qualification-*")
        self.assertEqual((stage / "stdout.log").read_text(), "retain\n")

    def test_interrupted_command_preserves_active_evidence(self) -> None:
        with mock.patch.object(record.subprocess, "run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                record.run(self.archive, ["fixture"])
        self.assertEqual(len(list(self.root.glob(".qualification-*"))), 1)
        self.assertFalse(self.archive.exists())

    def test_driver_cannot_overwrite_reserved_command_receipt(self) -> None:
        with self.assertRaises(FileExistsError):
            record.run(self.archive, [sys.executable, "-c", "import os; "
                       "from pathlib import Path; "
                       "Path(os.environ['OTELC_QUALIFICATION_DIR'], "
                       "'command.json').write_text('driver-owned')"])
        stage, = self.root.glob(".qualification-*")
        self.assertEqual((stage / "command.json").read_text(), "driver-owned")

    def test_run_refuses_reused_archive_and_maps_signal_exit(self) -> None:
        with mock.patch.object(record.subprocess, "run", return_value=
                               record.subprocess.CompletedProcess(["fixture"], -15)):
            self.assertEqual(record.run(self.archive, ["fixture"]), 143)
        with self.assertRaises(FileExistsError):
            record.run(self.archive, ["fixture"])

    def test_cli_pack_verify_restore_run(self) -> None:
        commands = [
            ["pack", str(self.source), str(self.archive)],
            ["verify", str(self.archive)],
            ["restore", str(self.archive), str(self.root / "cli-restored")],
            ["run", str(self.root / "cli.zip"), "--", sys.executable, "-c", "pass"],
        ]
        for args in commands:
            with self.subTest(args=args), mock.patch.object(sys, "argv", ["record", *args]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(record.main(), 0)


if __name__ == "__main__":
    unittest.main()
