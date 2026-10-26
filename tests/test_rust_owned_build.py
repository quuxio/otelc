"""Owned Rust envelopes bind existing inputs; literal tests start no compilers."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from otelc import native_gate, rust_build, rust_owned_build as owned
from otelc.config import Policy


class RustOwnedBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'original'
        self.source_dir = self.base / 'source-build'
        self.tools = self.base / 'compiler-build'
        self.sdk = self.base / 'sdk-build'
        for path in (self.root, self.source_dir, self.tools, self.sdk):
            path.mkdir()
        self.source_record = self.source_dir / rust_build.MANIFEST
        self.source_record.write_text('literal source-build test record\n')
        self.source = rust_build.Build(self.source_dir, self.source_record.read_text())
        self.metadata = {'profile': rust_build.PROFILE, 'root': str(self.root),
                         'tools': {'path': str(self.tools / 'rust-compiler-tools.json')},
                         'sdk': {'path': str(self.sdk / 'native-sdk-build.json')}}
        self.compiler = self.base / 'literal-compiler'
        self.compiler.write_bytes(b'not an executable compiler')
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['rust']})
        # The source verifier is separately tested with genuine compiler/SDK
        # products. These small literal cases test the envelope boundary only.
        verify = patch.object(rust_build, 'verify', return_value=self.metadata)
        verify.start()
        self.addCleanup(verify.stop)
        gate = patch.object(native_gate, 'build', side_effect=self.literal_gate)
        self.gate_builder = gate.start()
        self.addCleanup(gate.stop)
        storage = patch.object(owned.shutil, 'disk_usage', return_value=SimpleNamespace(free=9 * 1024**3))
        storage.start()
        self.addCleanup(storage.stop)
        process = patch('subprocess.Popen', side_effect=AssertionError('must not start a compiler/application'))
        process.start()
        self.addCleanup(process.stop)

    def literal_gate(self, output: Path) -> native_gate.Product:
        output.mkdir(mode=0o700)
        executable = output / 'otelc-exec-gate'
        executable.write_bytes(b'not an executable gate')
        value = {'schema_version': 1, 'kind': 'otelc.native.exec-gate',
                 'source': native_gate.identity(native_gate.SOURCE),
                 'compiler': native_gate.identity(self.compiler), 'executable': native_gate.identity(executable),
                 'coverage': False, 'command': [str(self.compiler), '-std=c11', '-O2', '-Wall', '-Wextra',
                    '-Werror', str(native_gate.SOURCE), '-o', str(executable)], 'source_complete': False}
        raw = json.dumps(value) + '\n'
        (output / 'gate.json').write_text(raw)
        # Match the genuine builder: its held JSON formatting differs from the
        # pretty-printed file. The envelope must pin the actual published bytes.
        return native_gate.Product(json.dumps(value, sort_keys=True))

    def build(self, name: str = 'owned') -> owned.Build:
        return owned.build(self.policy, self.source, self.base / name)

    def test_both_source_profiles_hold_original_records_and_return_fresh_views(self) -> None:
        original = native_gate.identity(self.source_record)
        for number, profile in enumerate((rust_build.PROFILE, rust_build.LIFETIME_PROFILE)):
            self.metadata['profile'] = profile
            value = self.build('owned-' + str(number))
            document = owned.verify(value, self.policy)
            self.assertEqual(document['source_profile'], profile)
            self.assertEqual(document['source_build'], original)
            self.assertIs(document['source_complete'], False)
            value.manifest()['gate'].clear()
            self.assertEqual(value.manifest(), document)
            self.assertEqual(native_gate.identity(self.source_record), original)
            self.assertEqual(value.directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(owned.read(value.directory, self.policy, source=self.source), value)
            with patch.object(rust_build, 'read', return_value=self.source) as reader:
                self.assertEqual(owned.read(value.directory, self.policy), value)
                reader.assert_called_once_with(self.source_dir, self.policy)
        self.assertEqual(self.gate_builder.call_count, 2)

    def test_output_source_build_tools_sdk_and_symlink_aliases_refuse_before_writes(self) -> None:
        alias = self.base / 'source-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        for parent in (self.root, self.source_dir, self.tools, self.sdk, alias):
            output = parent / 'owned'
            with self.subTest(parent=parent), self.assertRaisesRegex(ValueError, 'outside original'):
                owned.build(self.policy, self.source, output)
            self.assertFalse(output.exists())
        self.gate_builder.assert_not_called()

    def test_existing_output_and_low_storage_refuse_without_overwrite_or_compiler(self) -> None:
        output = self.base / 'owned'
        output.mkdir()
        sentinel = output / 'keep'
        sentinel.write_text('preserve')
        with self.assertRaises(FileExistsError):
            owned.build(self.policy, self.source, output)
        with patch.object(owned.shutil, 'disk_usage', return_value=SimpleNamespace(free=8 * 1024**3 - 1)), \
                self.assertRaisesRegex(ValueError, '8 GiB'):
            owned.build(self.policy, self.source, self.base / 'low-space')
        self.assertFalse((self.base / 'low-space').exists())
        self.assertEqual(sentinel.read_text(), 'preserve')
        self.gate_builder.assert_not_called()

    def test_changed_original_during_gate_build_does_not_publish_an_envelope(self) -> None:
        def changed(output: Path) -> native_gate.Product:
            result = self.literal_gate(output)
            self.source_record.write_text('changed original test record')
            return result
        self.gate_builder.side_effect = changed
        with self.assertRaisesRegex(ValueError, 'changed during'):
            self.build()
        self.assertFalse((self.base / 'owned' / owned.MANIFEST).exists())

    def test_gate_publication_must_match_the_compiled_product(self) -> None:
        def changed(output: Path) -> native_gate.Product:
            result = self.literal_gate(output)
            (output / 'gate.json').write_text('{}')
            return result
        self.gate_builder.side_effect = changed
        with self.assertRaisesRegex(ValueError, 'publication differs'):
            self.build()
        self.assertFalse((self.base / 'owned' / owned.MANIFEST).exists())

    def test_held_envelope_and_original_file_identity_changes_refuse(self) -> None:
        value = self.build()
        path = value.directory / owned.MANIFEST
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'record changed'):
            owned.verify(value, self.policy)
        self.source_record.write_text(self.source_record.read_text() + 'changed')
        with self.assertRaisesRegex(ValueError, 'source-build identity changed'):
            owned.read(value.directory, self.policy)
        with self.assertRaises(ValueError):
            owned.verify(None, self.policy)

    def test_unknown_headers_and_changed_source_profile_refuse(self) -> None:
        value = self.build()
        original = value.manifest()
        path = value.directory / owned.MANIFEST
        for key, changed in (('schema_version', True), ('schema_version', 1.0), ('kind', 'other'),
                             ('profile', 'other'), ('source_profile', 'other'), ('source_complete', 0),
                             ('source_build', {}), ('source_build', None), ('extra', False)):
            path.write_text(json.dumps(original | {key: changed}))
            with self.subTest(key=key), patch.object(rust_build, 'read', side_effect=AssertionError('early refusal')), \
                    self.assertRaises(ValueError):
                owned.read(value.directory, self.policy)
        path.write_text(value.encoded)
        self.metadata['profile'] = rust_build.LIFETIME_PROFILE
        with self.assertRaisesRegex(ValueError, 'original binding changed'):
            owned.verify(value, self.policy)

    def test_declared_original_must_be_fixed_canonical_and_match_held_source(self) -> None:
        value = self.build()
        original = value.manifest()
        path = value.directory / owned.MANIFEST
        for name in ('relative/rust-source-build.json', str(self.compiler)):
            changed = original | {'source_build': original['source_build'] | {'path': name}}
            path.write_text(json.dumps(changed))
            with self.subTest(name=name), self.assertRaises(ValueError):
                owned.read(value.directory, self.policy)
        path.write_text(value.encoded)
        with self.assertRaisesRegex(ValueError, 'caller-held source'):
            owned.read(value.directory, self.policy, source=replace(self.source, directory=self.tools))

    def test_rehashed_gate_metadata_cannot_change_fixed_recipe_source_or_image(self) -> None:
        value = self.build()
        path = value.directory / 'gate/gate.json'
        original = value.gate.metadata()
        for key, changed in (('schema_version', True), ('kind', 'other'), ('coverage', True),
                             ('source_complete', 0), ('source', native_gate.identity(self.compiler)),
                             ('executable', native_gate.identity(self.compiler)), ('compiler', {}),
                             ('command', ['arbitrary-command']), ('extra', False)):
            raw = json.dumps(original | {key: changed})
            path.write_text(raw)
            document = value.manifest() | {'gate': native_gate.identity(path)}
            encoded = json.dumps(document)
            (value.directory / owned.MANIFEST).write_text(encoded)
            changed_build = replace(value, encoded=encoded, gate=native_gate.Product(raw))
            with self.subTest(key=key), self.assertRaises(ValueError):
                owned.verify(changed_build, self.policy)

    def test_changed_gate_record_or_image_refuses_without_rebuild(self) -> None:
        value = self.build()
        path = value.directory / 'gate/gate.json'
        path.write_text(path.read_text() + ' ')
        encoded = json.dumps(value.manifest() | {'gate': native_gate.identity(path)})
        (value.directory / owned.MANIFEST).write_text(encoded)
        with self.assertRaisesRegex(ValueError, 'gate record changed'):
            owned.verify(replace(value, encoded=encoded), self.policy)
        path.write_text(value.gate.binding)
        encoded = json.dumps(value.manifest() | {'gate': native_gate.identity(path)})
        (value.directory / owned.MANIFEST).write_text(encoded)
        (value.directory / 'gate/otelc-exec-gate').write_bytes(b'changed product')
        with self.assertRaisesRegex(ValueError, 'fixed version-one'):
            owned.verify(replace(value, encoded=encoded), self.policy)


if __name__ == '__main__':
    unittest.main()
