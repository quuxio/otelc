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
    def test_build_reuse_and_changed_products_or_manifest_are_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            parent = Path(temporary)
            binary = tooling.build(parent)
            original = binary.read_bytes()
            self.assertEqual(tooling.build(parent), binary)
            manifest_path = binary.parent / 'build.json'
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest['inputs']['version'], sys.version)
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

