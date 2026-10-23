"""Native products are reused only with exact inputs and complete original bytes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ci import build_python_inputs as tooling


class PythonInputBuildTests(unittest.TestCase):
    def test_initial_executable_probe_is_isolated_and_records_real_file_identity(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            root = Path(temporary)
            marker = root / 'startup-ran'
            (root / 'sitecustomize.py').write_text(
                'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
            with mock.patch.dict(os.environ, {'PYTHONPATH': str(root)}):
                identities = tooling.initial_configuration()
            self.assertFalse(marker.exists())
            self.assertEqual(set(identities), {'executable', 'base_executable'})
            for identity in identities.values():
                self.assertTrue(Path(identity['path']).samefile(sys.executable))
                self.assertEqual(identity, tooling.digest(Path(identity['path'])))

    def test_initial_executable_probe_cannot_select_another_interpreter_or_relative_path(self) -> None:
        valid = {'executable': sys.executable, 'base_executable': sys._base_executable}
        invalid = [None, [], True, '', {}, {**valid, 'extra': 'unknown'}]
        for name in valid:
            invalid.extend({**valid, name: value} for value in ('', True, 'relative/python', '/nul\0python'))
        for value in invalid:
            with self.subTest(value=value), mock.patch.object(tooling.subprocess, 'check_output',
                    return_value=json.dumps(value)), self.assertRaisesRegex(ValueError, 'probe is invalid'):
                tooling.initial_configuration()
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            other = Path(temporary) / 'other-python'
            other.write_bytes(b'foreign interpreter')
            with mock.patch.object(tooling.subprocess, 'check_output', return_value=json.dumps({**valid, 'executable': str(other)})), \
                    self.assertRaisesRegex(ValueError, 'differs from the selected'):
                tooling.initial_configuration()

    def test_copied_venv_with_spaces_and_unicode_keeps_native_metadata(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            environment = Path(temporary).resolve() / 'copied caf\u00e9 environment'
            subprocess.run([sys.executable, '-I', '-S', '-B', '-m', 'venv',
                '--without-pip', '--copies', str(environment)], check=True, capture_output=True, timeout=30)
            python = environment / 'bin/python'
            bootstrap = ('import sys,unittest;sys.path[:0]=sys.argv[1:3];'
                         'suite=unittest.defaultTestLoader.loadTestsFromName('
                         '"test_python_native_inputs.PythonNativeInputTests.'
                         'test_relative_unicode_symlink_flags_and_entry_metadata_remain_native");'
                         'result=unittest.TextTestRunner(verbosity=2).run(suite);'
                         'sys.exit(not result.wasSuccessful())')
            result = subprocess.run([str(python), '-I', '-S', '-B', '-c', bootstrap,
                str(tooling.ROOT), str(tooling.ROOT / 'tests')], capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_equivalent_header_directory_spelling_does_not_change_the_build_identity(self) -> None:
        original, flags = tooling.toolchain()
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            alias = Path(temporary) / 'include-alias'
            alias.symlink_to(tooling.sysconfig.get_path('include'), target_is_directory=True)
            with mock.patch.object(tooling.sysconfig, 'get_path', return_value=str(alias)):
                selected, selected_flags = tooling.toolchain()
        self.assertEqual(flags, selected_flags)
        self.assertTrue(original == selected, 'header path spelling changed the exact build identity')

    def test_build_reuse_and_changed_products_or_manifest_are_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            parent = Path(temporary)
            binary = tooling.build(parent)
            original = binary.read_bytes()
            self.assertEqual(tooling.build(parent), binary)
            manifest_path = binary.parent / 'build.json'
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest['inputs']['version'], sys.version)
            generated = binary.parent / 'EncoderFactory.h'
            original_header = generated.read_bytes()
            self.assertEqual(manifest['products']['EncoderFactory.h']['sha256'],
                             manifest['inputs']['encoder']['header_sha256'])
            generated.write_bytes(original_header + b'changed')
            with self.assertRaisesRegex(ValueError, 'product changed'):
                tooling.build(parent)
            generated.write_bytes(original_header)
            manifest_path.write_text(json.dumps({**manifest, 'library': '../foreign'}))
            with self.assertRaisesRegex(ValueError, 'inputs differ'):
                tooling.build(parent)
            manifest_path.write_text(json.dumps(manifest))
            binary.write_bytes(original + b'altered')
            with self.assertRaisesRegex(ValueError, 'product changed'):
                tooling.build(parent)
            binary.write_bytes(original)
            link = binary.parent / 'foreign'
            binary.rename(link)
            binary.symlink_to(link.name)
            with self.assertRaisesRegex(ValueError, 'product changed'):
                tooling.build(parent)

    def test_missing_compilers_and_unsupported_interpreters_fail_before_building(self) -> None:
        with mock.patch.dict(os.environ, {'CC': 'unavailable-otelc-compiler'}), \
                mock.patch.object(tooling.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'one installed'):
                tooling.compiler()
        with mock.patch.dict(os.environ, {'CC': ''}), \
                mock.patch.object(Path, 'is_file', return_value=False), \
                mock.patch.object(tooling.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Clang is required'):
                tooling.compiler()
        with mock.patch.object(tooling.sys, 'implementation', SimpleNamespace(name='pypy')):
            with self.assertRaisesRegex(ValueError, 'GIL CPython'):
                tooling.toolchain()
        with mock.patch.object(tooling.sys, 'platform', 'unsupported'):
            with self.assertRaisesRegex(ValueError, 'GIL CPython'):
                tooling.toolchain()
        actual_config = tooling.sysconfig.get_config_var
        with mock.patch.object(tooling.sysconfig, 'get_config_var',
                side_effect=lambda name: 0 if name in ('Py_ENABLE_SHARED', 'PYTHONFRAMEWORK') else actual_config(name)):
            with self.assertRaisesRegex(ValueError, 'shared/framework'):
                tooling.toolchain()

    def test_headers_must_describe_the_exact_selected_version(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            include = Path(temporary)
            for text in ('#define PY_VERSION "0.0.0"\n', 'missing macro\n'):
                (include / 'patchlevel.h').write_text(text)
                with mock.patch.object(tooling.sysconfig, 'get_path', return_value=str(include)):
                    with self.assertRaisesRegex(ValueError, 'exact interpreter'):
                        tooling.toolchain()

    def test_native_input_change_during_compilation_cannot_publish_a_product(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            root = Path(temporary)
            native = root / 'native/python'
            native.mkdir(parents=True)
            for name in tooling.SOURCES:
                shutil.copyfile(tooling.ROOT / 'native/python' / name, native / name)
            (root / 'otelc').mkdir()
            shutil.copyfile(tooling.ROOT / 'otelc/python_code_descriptor.py',
                            root / 'otelc/python_code_descriptor.py')
            actual_run = subprocess.run
            completed = []

            def compile_then_change(command, **kwargs):
                result = actual_run(command, **kwargs)
                if any(str(native / name) in command for name in ('EntryCapture.c', 'NativeMain.c')):
                    completed.append(command)
                    if len(completed) == 1:
                        header = native / 'InputJournal.h'
                        header.write_text(header.read_text() + '\n/* changed after the first compilation */\n')
                return result

            output = root / 'products'
            with mock.patch.object(tooling, 'ROOT', root), \
                    mock.patch.object(tooling.subprocess, 'run', side_effect=compile_then_change):
                with self.assertRaisesRegex(ValueError, 'changed while compiling'):
                    tooling.build(output)
            self.assertEqual(len(completed), 2)
            self.assertFalse(list(output.iterdir()))

