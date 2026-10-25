"""Backports refuse source drift and retain their complete private source copy."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ci import go_scope_patch as backport


class GoScopePatchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.module = self.root / 'verified-module'
        self.output = self.root / 'installation'
        self.output.mkdir()
        self.target = self.module / backport.FILE
        self.target.parent.mkdir(parents=True)
        self.original = backport.ANCHOR + b'\treturn writeHookContext()\n}\n'
        self.target.write_bytes(self.original)
        self.target.chmod(0o444)
        (self.module / 'go.mod').write_text('module go.opentelemetry.io/otelc\n')
        self.result = self.original.replace(backport.ANCHOR, backport.ANCHOR + backport.GUARD)
        for name, value in (('BEFORE', backport.digest(self.original)), ('AFTER', backport.digest(self.result))):
            selected = patch.object(backport, name, value)
            selected.start()
            self.addCleanup(selected.stop)

    def install(self) -> str:
        backport.prepare(self.module, self.output)
        return backport.digest((self.output / backport.MANIFEST).read_bytes())

    def test_private_copy_preserves_base_and_changes_only_the_declared_file(self) -> None:
        before = backport.inventory(self.module)
        checksum = self.install()
        source = self.output / 'source'
        self.assertEqual((source / backport.FILE).read_bytes(), self.result)
        self.assertEqual(backport.inventory(self.module), before)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o444)
        backport.verify(self.output, checksum)
        with self.assertRaises(FileExistsError):
            backport.prepare(self.module, self.output)
        self.assertEqual((source / backport.FILE).read_bytes(), self.result)

    def test_wrong_release_or_unexpected_patch_result_is_refused_before_copy(self) -> None:
        with self.assertRaisesRegex(ValueError, 'pinned upstream'):
            backport.patched(self.original + b'// changed\n')
        with patch.object(backport, 'AFTER', '0' * 64), self.assertRaisesRegex(ValueError, 'reviewed result'):
            backport.prepare(self.module, self.output)
        self.assertFalse((self.output / 'source').exists())
        for raw in (b'no anchor', self.original + self.original):
            with patch.object(backport, 'BEFORE', backport.digest(raw)), \
                    self.assertRaisesRegex(ValueError, 'pinned upstream'):
                backport.patched(raw)

    def test_recertified_extra_missing_or_changed_sources_are_refused(self) -> None:
        checksum = self.install()
        manifest = self.output / backport.MANIFEST
        raw = manifest.read_bytes()
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            backport.verify(self.output, '0' * 64)
        value = json.loads(raw)
        for key, wrong in (('schema_version', True), ('profile', 'other'),
                           ('patch', {}), ('base_files', {}), ('files', {}), ('extra', 0)):
            manifest.write_text(json.dumps(value | {key: wrong}))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'fixed profile'):
                backport.verify(self.output, backport.digest(manifest.read_bytes()))
        manifest.write_bytes(raw)
        source = self.output / 'source'
        target = source / 'go.mod'
        original = target.read_bytes()
        target.chmod(0o600)
        target.write_bytes(original + b'// changed\n')
        with self.assertRaisesRegex(ValueError, 'fixed profile'):
            backport.verify(self.output, checksum)
        target.write_bytes(original)
        target.unlink()
        with self.assertRaisesRegex(ValueError, 'fixed profile'):
            backport.verify(self.output, checksum)
        target.write_bytes(original)
        (source / 'unexpected.go').write_text('package unexpected\n')
        with self.assertRaisesRegex(ValueError, 'fixed profile'):
            backport.verify(self.output, checksum)

    def test_symbolic_files_and_directories_are_never_followed(self) -> None:
        (self.module / 'outside').symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'regular files'):
            self.install()
        (self.module / 'outside').unlink()
        link = self.root / 'module-link'
        link.symlink_to(self.module, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'real directory'):
            backport.inventory(link)

    def test_source_change_during_copy_is_detected(self) -> None:
        original = backport.shutil.copytree

        def copying(source: Path, target: Path, *arguments, **options) -> None:
            original(source, target, *arguments, **options)
            if Path(source) == self.module:
                (self.module / 'go.mod').write_text('module changed\n')

        with patch.object(backport.shutil, 'copytree', side_effect=copying), \
                self.assertRaisesRegex(ValueError, 'changed while'):
            self.install()
        self.assertFalse((self.output / backport.MANIFEST).exists())
