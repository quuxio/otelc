"""Tests for bounded periodic qualification retirement."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qualification_cleanup as cleanup
import qualification_record as record

DAY = 86400 * 1_000_000_000


class QualificationCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "data/build-cache"
        self.path.mkdir(parents=True)
        (self.path / "object").write_bytes(b"owned fixture")
        self.registry = {"schema_version": 1, "pins": ["work"], "candidates": {}}
        self.archive = self.root / "backups/record.zip"
        self.archive.parent.mkdir()
        self.future = time.time_ns() + 15 * DAY
        patcher = mock.patch.object(cleanup, "in_use", return_value=False)
        self.open_check = patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, kind="cache", archive=None) -> None:
        cleanup.register(self.root, self.registry, "data/build-cache", kind,
                         "fixture reference review completed", True, archive)

    def clean(self, apply=False) -> list:
        with mock.patch.object(cleanup.time, "time_ns", return_value=self.future):
            return cleanup.clean(self.root, self.registry, apply)

    def test_age_wait_and_dry_run_then_retire_cache(self) -> None:
        self.register()
        self.assertEqual(cleanup.clean(self.root, self.registry)[0]["status"], "waiting")
        self.assertEqual(self.clean()[0]["status"], "eligible")
        self.assertTrue(self.path.exists())
        self.open_check.assert_not_called()
        self.assertEqual(self.clean(True)[0]["status"], "removed")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.registry["candidates"], {})

    def test_cache_and_expanded_have_distinct_minimum_ages(self) -> None:
        record.pack(self.path, self.archive)
        self.register("expanded", self.archive)
        with mock.patch.object(cleanup.time, "time_ns", return_value=time.time_ns() + 8 * DAY):
            self.assertEqual(cleanup.clean(self.root, self.registry, True)[0]["status"], "waiting")
        self.assertEqual(self.clean(True)[0]["status"], "removed")
        record.verify(self.archive)

    def test_no_implicit_age_only_targets(self) -> None:
        self.assertEqual(self.clean(True), [])
        self.assertTrue(self.path.is_dir())

    def test_confirmation_reason_and_supported_class_required(self) -> None:
        for confirmed, reason, kind in ((False, "reviewed", "cache"),
                                        (True, " ", "cache"),
                                        (True, "reviewed", "unknown")):
            with self.subTest(kind=kind, confirmed=confirmed, reason=reason):
                with self.assertRaises(ValueError):
                    cleanup.register(self.root, self.registry, "data/build-cache",
                                     kind, reason, confirmed)

    def test_mixed_build_and_source_roots_are_not_cache_targets(self) -> None:
        (self.root / "data/build").mkdir()
        with self.assertRaisesRegex(ValueError, "Mixed"):
            cleanup.register(self.root, self.registry, "data/build", "cache",
                             "reviewed", True)

    def test_go_cache_build_leaf_is_supported(self) -> None:
        path = self.root / "data/go-cache/build"
        path.mkdir(parents=True)
        cleanup.register(self.root, self.registry, "data/go-cache/build", "cache",
                         "all referenced compiler inputs retained separately", True)

    def test_repeat_admission_does_not_reset_the_review(self) -> None:
        self.register()
        with self.assertRaisesRegex(ValueError, "Already"):
            self.register()

    def test_changed_file_or_member_set_blocks_retirement(self) -> None:
        self.register()
        (self.path / "new").write_text("new")
        self.assertEqual(self.clean(True)[0]["status"], "blocked")
        self.assertTrue(self.path.exists())

    def test_new_pin_blocks_an_existing_candidate(self) -> None:
        self.register()
        self.registry["pins"].append("data/build-cache/object")
        self.assertEqual(self.clean(True)[0]["status"], "blocked")

    def test_pinned_work_and_invalid_names_are_refused(self) -> None:
        (self.root / "work/build-cache").mkdir(parents=True)
        for name in ("work/build-cache", "data", "../escape", "/absolute", "data//x"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                cleanup.target(self.root, name, self.registry["pins"])
        with self.assertRaisesRegex(ValueError, "Invalid pin"):
            cleanup.target(self.root, "data/build-cache", ["../bad"])

    def test_symlink_and_missing_targets_are_refused(self) -> None:
        (self.root / "link").symlink_to(self.root / "data")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            cleanup.target(self.root, "link/build-cache", [])
        with self.assertRaisesRegex(ValueError, "existing"):
            cleanup.target(self.root, "data/missing", [])

    def test_expanded_record_requires_exact_surviving_archive(self) -> None:
        with self.assertRaisesRegex(ValueError, "require"):
            self.register("expanded")
        with self.assertRaisesRegex(ValueError, "survive"):
            self.register("expanded", self.path / "archive.zip")
        record.pack(self.path, self.archive)
        (self.path / "object").write_bytes(b"changed data")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.register("expanded", self.archive)

    def test_archive_member_set_and_metadata_must_match(self) -> None:
        record.pack(self.path, self.archive)
        (self.path / "extra").touch()
        with self.assertRaisesRegex(ValueError, "member set"):
            cleanup.archive_matches(self.path, self.archive)

    def test_same_size_content_change_is_not_accepted(self) -> None:
        record.pack(self.path, self.archive)
        original = (self.path / "object").stat()
        (self.path / "object").write_bytes(b"other fixture")
        os.utime(self.path / "object", ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, "content"):
            self.register("expanded", self.archive)

    def test_corrupt_archive_blocks_retirement(self) -> None:
        record.pack(self.path, self.archive)
        self.register("expanded", self.archive)
        with self.archive.open("ab") as stream:
            stream.write(b"corruption")
        self.assertEqual(self.clean(True)[0]["status"], "blocked")
        self.assertTrue(self.path.exists())

    def test_backing_archives_are_protected_from_other_candidates(self) -> None:
        archive = self.root / "data/compiler-cache/record.zip"
        archive.parent.mkdir()
        record.pack(self.path, archive)
        self.register("expanded", archive)
        with self.assertRaisesRegex(ValueError, "pin"):
            cleanup.register(self.root, self.registry, "data/compiler-cache",
                             "cache", "reviewed", True)

    def test_archive_cannot_be_placed_under_an_admitted_cache(self) -> None:
        self.register()
        path = self.root / "data/expanded"
        path.mkdir()
        with self.assertRaisesRegex(ValueError, "another"):
            cleanup.register(self.root, self.registry, "data/expanded", "expanded",
                             "reviewed", True, self.path / "archive.zip")

    def test_open_files_and_inspection_failure_block_deletion(self) -> None:
        self.register()
        self.open_check.return_value = True
        self.assertEqual(self.clean(True)[0]["status"], "blocked")
        self.open_check.side_effect = subprocess.TimeoutExpired("lsof", 30)
        self.assertEqual(self.clean(True)[0]["status"], "blocked")
        self.assertTrue(self.path.exists())

    def test_partial_delete_error_is_reported_and_registration_is_kept(self) -> None:
        self.register()
        with mock.patch.object(cleanup.shutil, "rmtree", side_effect=OSError("busy")):
            self.assertEqual(self.clean(True)[0]["status"], "blocked")
        self.assertIn("data/build-cache", self.registry["candidates"])

    def test_bad_registry_entry_is_blocked(self) -> None:
        self.register()
        self.registry["candidates"]["data/build-cache"]["kind"] = "unknown"
        self.assertEqual(self.clean(True)[0]["status"], "blocked")

    def test_open_file_probe_is_fail_closed(self) -> None:
        # Use the real function despite the deletion fixture's default mock.
        with mock.patch.object(cleanup.subprocess, "run") as runner:
            for result, expected in ((subprocess.CompletedProcess([], 0, "123\n", ""), True),
                                     (subprocess.CompletedProcess([], 1, "", ""), False)):
                runner.return_value = result
                self.assertEqual(REAL_IN_USE(self.path), expected)
            runner.return_value = subprocess.CompletedProcess([], 1, "", "incomplete")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                REAL_IN_USE(self.path)

    def test_atomic_registry_failure_preserves_previous_file(self) -> None:
        cleanup.save(self.root, self.registry)
        before = (self.root / cleanup.REGISTRY).read_bytes()
        with mock.patch.object(cleanup.os, "replace", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                cleanup.save(self.root, self.registry)
        self.assertEqual((self.root / cleanup.REGISTRY).read_bytes(), before)
        self.assertEqual(list(self.root.glob(".retention-*")), [])

    def test_cli_registration_dry_run_and_apply(self) -> None:
        cleanup.save(self.root, self.registry)
        commands = [["register", "data/build-cache", "--kind", "cache",
                     "--reason", "reviewed", "--confirmed-unused"],
                    ["clean"], ["clean", "--apply"]]
        for command in commands:
            with mock.patch.object(sys, "argv", ["cleanup", "--root", str(self.root), *command]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cleanup.main(), 0)
        self.assertTrue(self.path.exists())

    def test_cli_refuses_unknown_registry_format(self) -> None:
        self.registry["schema_version"] = 2
        cleanup.save(self.root, self.registry)
        with mock.patch.object(sys, "argv", ["cleanup", "--root", str(self.root), "clean"]):
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                cleanup.main()


REAL_IN_USE = cleanup.in_use


if __name__ == "__main__":
    unittest.main()
