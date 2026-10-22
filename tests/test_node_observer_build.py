"""Native observer builds bind to exact public headers and runtime ABI."""

from __future__ import annotations

import json
import os
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from ci.build_node_observer import build, matching_headers


def headers(directory: Path, version: str = '24.21.0', abi: str = '137') -> None:
    """Use the same defines exported by actual Node distribution headers."""
    directory.mkdir(exist_ok=True)
    major, minor, patch_version = version.split('.')
    (directory / 'node_version.h').write_text('\n'.join(f'#define NODE_{name} {value}' for name, value in
        (('MAJOR_VERSION', major), ('MINOR_VERSION', minor), ('PATCH_VERSION', patch_version), ('MODULE_VERSION', abi))))


class NodeObserverBuildTests(unittest.TestCase):
    def test_failed_rebuild_preserves_the_previously_loadable_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            include = directory / 'headers'
            headers(include)
            library = directory / 'build/tools/otelc_node_observer-24.21.0.node'
            library.parent.mkdir(parents=True)
            library.write_bytes(b'previous loadable binary')
            info = json.dumps({'version': '24.21.0', 'abi': '137', 'executable': '/node/bin/node'})

            def failed_compile(command: list[str], **_kwargs: object) -> None:
                Path(command[command.index('-o') + 1]).write_bytes(b'incomplete replacement')
                raise subprocess.CalledProcessError(1, command)

            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch.dict(os.environ, {'OTELC_NODE': '/node', 'OTELC_NODE_INCLUDE': str(include)}, clear=True), \
                        patch('ci.build_node_observer.subprocess.check_output', return_value=info), \
                        patch('ci.build_node_observer.subprocess.run', side_effect=failed_compile):
                    with self.assertRaises(subprocess.CalledProcessError):
                        build()
                self.assertEqual(library.read_bytes(), b'previous loadable binary')
            finally:
                os.chdir(previous)

    def test_exact_match_is_accepted_but_other_patch_abi_or_incomplete_headers_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            include = Path(temporary)
            headers(include)
            matching_headers(include, '24.21.0', '137')
            with self.assertRaisesRegex(ValueError, 'exact running version'):
                matching_headers(include, '24.21.1', '137')
            with self.assertRaisesRegex(ValueError, 'exact running version'):
                matching_headers(include, '24.21.0', '138')
            (include / 'node_version.h').write_text('#define NODE_MAJOR_VERSION 24\n')
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                matching_headers(include, '24.21.0', '137')

    def test_missing_or_unsupported_runtime_is_refused_before_compilation(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch('ci.build_node_observer.shutil.which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Node 24.11'):
                build()
        info = json.dumps({'version': '22.20.0', 'abi': '127', 'executable': '/node/bin/node'})
        with patch.dict(os.environ, {'OTELC_NODE': '/node/bin/node'}, clear=True), \
                patch('ci.build_node_observer.subprocess.check_output', return_value=info), \
                patch('ci.build_node_observer.subprocess.run') as compiler:
            with self.assertRaisesRegex(ValueError, 'Node 24.11'):
                build()
            compiler.assert_not_called()

    def test_build_has_versioned_output_and_explicit_platform_and_coverage_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            include = directory / 'headers'
            headers(include)
            info = json.dumps({'version': '24.21.0', 'abi': '137', 'executable': '/node/bin/node'})
            environment = {'OTELC_NODE': '/node/bin/node', 'OTELC_NODE_INCLUDE': str(include), 'CXX': '/clang++'}
            previous = Path.cwd()
            def compile_candidate(command: list[str], **_kwargs: object) -> None:
                Path(command[command.index('-o') + 1]).write_bytes(b'complete candidate')
            try:
                os.chdir(directory)
                for platform, instrumented in (('darwin', True), ('linux', False)):
                    with self.subTest(platform=platform), patch.dict(os.environ,
                        {**environment, **({'OTELC_NATIVE_COVERAGE': '1'} if instrumented else {})}, clear=True), \
                            patch('ci.build_node_observer.sys.platform', platform), \
                            patch('ci.build_node_observer.subprocess.check_output', return_value=info), \
                            patch('ci.build_node_observer.subprocess.run', side_effect=compile_candidate) as compiler:
                        self.assertEqual(build(), Path('build/tools/otelc_node_observer-24.21.0.node'))
                        command = compiler.call_args.args[0]
                        self.assertEqual(command[0], '/clang++')
                        self.assertEqual('-fcoverage-mapping' in command, instrumented)
                        self.assertEqual('dynamic_lookup' in command, platform == 'darwin')
                        self.assertEqual(Path('build/tools/otelc_node_observer-24.21.0.node').read_bytes(), b'complete candidate')
            finally:
                os.chdir(previous)
