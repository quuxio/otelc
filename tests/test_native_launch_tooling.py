"""Build refusal, compiler diagnostics and escaped native input records."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from otelc import native_launch
from otelc.config import Policy


class NativeLaunchToolingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def test_missing_build_tools_fail_before_creating_output(self) -> None:
        output = self.base / 'sdk'
        with patch('otelc.llvm_backend.discover', return_value={}), patch('shutil.which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'CMake and Ninja'):
                native_launch.build_sdk(output)
        self.assertFalse(output.exists())

    def test_invalid_sources_and_output_are_refused_before_compiler_execution(self) -> None:
        root = self.base / 'app'
        root.mkdir()
        source = root / 'main.c'
        source.write_text('int main(){return 0;}\n')
        policy = Policy.parse({'schema_version': 3})
        for sources, output in (([], self.base / 'none'), ([source, source], self.base / 'duplicate'),
                                ([source], root / 'build')):
            with self.subTest(sources=sources, output=output), self.assertRaises(ValueError):
                native_launch.build(policy, 'c', root, sources, output, library=Path(sys.executable))
            self.assertFalse(output.exists())

    def test_compiler_failure_retains_original_channels(self) -> None:
        command = [sys.executable, '-c', 'import sys;print("output");print("original error",file=sys.stderr);sys.exit(7)']
        with self.assertRaisesRegex(ValueError, 'failed; see'):
            native_launch._compile(command, self.base, self.base, 'failed', dict(os.environ))
        self.assertEqual((self.base / 'failed.stdout').read_bytes(), b'output\n')
        self.assertEqual((self.base / 'failed.stderr').read_bytes(), b'original error\n')

    def test_make_dependency_escapes_preserve_dollars_and_spaces(self) -> None:
        first = self.base / 'source name.c'
        second = self.base / 'cost$one.h'
        first.write_bytes(b'original source')
        second.write_bytes(b'original header')
        deps = self.base / 'input.d'
        deps.write_text('otelc-inputs: source\\ name.c \\\n cost$$one.h\n')
        self.assertEqual([row['path'] for row in native_launch._inputs(deps, self.base)], [str(first), str(second)])
        deps.write_text('foreign-target: source\\ name.c\n')
        with self.assertRaisesRegex(ValueError, 'dependency output'):
            native_launch._inputs(deps, self.base)

    def test_sdk_recipe_is_fixed_and_retains_the_chosen_product(self) -> None:
        output = self.base / 'sdk'
        library = output / ('libotelc_native_runtime.dylib' if sys.platform == 'darwin' else 'libotelc_native_runtime.so')
        tools = {'directory': '/selected/llvm22'}
        commands = []

        def compile_(command, root, directory, label, environment, **options):
            commands.append(command)
            self.assertEqual(root, native_launch.ROOT)
            if label == 'sdk-build':
                library.write_bytes(b'unit fixture product; actual SDK builds have separate integration checks')
                self.assertEqual(options['timeout'], 900)

        with patch('otelc.llvm_backend.discover', return_value=tools), \
                patch('otelc.llvm_backend.unchanged', return_value=True), \
                patch('shutil.which', return_value=sys.executable), \
                patch('otelc.native_launch._compile', side_effect=compile_):
            result = native_launch.build_sdk(output, coverage=True)
        self.assertIn('-DCMAKE_C_COMPILER=/selected/llvm22/clang', commands[0])
        self.assertIn('-DCMAKE_CXX_COMPILER=/selected/llvm22/clang++', commands[0])
        self.assertIn('-DOTELC_LAUNCH_COVERAGE=ON', commands[0])
        self.assertEqual(result['library']['path'], str(library))
        self.assertFalse(result['source_complete'])
        self.assertEqual(json.loads((output / 'native-sdk-build.json').read_text()), result)

    def test_sdk_cli_forwards_explicit_output_and_coverage(self) -> None:
        with patch('sys.argv', ['native_launch', '--output', str(self.base / 'sdk'), '--coverage']), \
                patch('otelc.native_launch.build_sdk', return_value={'kind': 'test'}) as build:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                native_launch.main()
        build.assert_called_once_with(self.base / 'sdk', coverage=True)
        self.assertEqual(json.loads(stream.getvalue()), {'kind': 'test'})
