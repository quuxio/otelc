"""Keep public Go build and launch outputs outside physical source inputs."""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ci import build_go_upstream
from otelc import go_build, go_source, path_boundaries, prepared_go
from otelc.__main__ import main
from otelc.config import Policy


class GoOutputBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'Application'
        self.root.mkdir()
        (self.root / 'go.mod').write_text('module example.test/original\n\ngo 1.26.0\n')
        (self.root / 'main.go').write_text('package main\nfunc main() {}\n')
        self.alias = self.alias_for(self.root)
        self.build = self.base / 'Build'
        self.build.mkdir()
        (self.build / 'private').mkdir()
        self.build_alias = self.alias_for(self.build)

    def alias_for(self, path: Path) -> Path:
        alias = path.with_name(path.name.swapcase())
        if not alias.exists():
            alias.symlink_to(path, target_is_directory=True)
        self.assertTrue(alias.samefile(path))
        return alias

    def test_build_refuses_source_alias_before_input_copy_or_compiler(self) -> None:
        for directory in (self.root, self.alias):
            output = directory / 'generated'
            with self.subTest(directory=directory), \
                    mock.patch.object(go_build, 'inputs', return_value={}), \
                    mock.patch.object(go_source, 'inspect_project', side_effect=AssertionError('compiler sentinel')), \
                    self.assertRaisesRegex(ValueError, 'external output'):
                go_build.build_project(self.root, '.', Policy.parse({'schema_version': 3}), output, {})
            self.assertFalse(output.exists())

    def test_inspector_refuses_source_alias_before_compiler_setup(self) -> None:
        for directory in (self.root, self.alias):
            output = directory / 'inspection'
            with self.subTest(directory=directory), \
                    mock.patch.object(build_go_upstream, 'compiler_environment',
                                      side_effect=AssertionError('compiler setup sentinel')), \
                    self.assertRaisesRegex(ValueError, 'outside original application inputs'):
                go_source.inspect_project(self.root, '.', {}, output)
            self.assertFalse(output.exists())

    def test_preparation_refuses_source_and_private_build_aliases_before_request(self) -> None:
        for directory in (self.root, self.alias, self.build / 'private', self.build_alias / 'private'):
            runtime = directory / 'runtime'
            with self.subTest(directory=directory), mock.patch.object(prepared_go, 'ambient'), \
                    mock.patch.object(prepared_go, 'verify_build', return_value={'receipt': {'root': str(self.root)}}), \
                    mock.patch.object(prepared_go, 'request', side_effect=AssertionError('request sentinel')), \
                    self.assertRaisesRegex(ValueError, 'outside checked source/build input trees'):
                prepared_go.prepare(Policy.parse({'schema_version': 3}), runtime,
                                    build_directory=self.build, plain=True)
            self.assertFalse(runtime.exists())

    def test_cli_refuses_output_or_tooling_source_alias_before_tool_install_or_reuse(self) -> None:
        for prepared in (False, True):
            for kind in ('output', 'tooling'):
                destination = self.alias / 'generated'
                options = ['quux-otelc', 'go-build', '--root', str(self.root),
                    '--output', str(destination if kind == 'output' else self.base / 'output')]
                if kind == 'tooling':
                    options += ['--tooling-directory', str(destination)]
                if prepared:
                    options += ['--prepared-tools']
                with self.subTest(prepared=prepared, kind=kind):
                    with mock.patch('sys.argv', options), \
                            mock.patch.object(build_go_upstream, 'build', side_effect=AssertionError('installer sentinel')), \
                            mock.patch('otelc.go_prepared_tools.prepare', side_effect=AssertionError('reuse sentinel')), \
                            contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                        main()
                    self.assertEqual(error.exception.code, 1)
                    self.assertFalse(destination.exists())

    def test_external_new_paths_and_siblings_with_shared_names_are_allowed(self) -> None:
        for path in (self.base / 'Application-copy/output', self.base / 'new/nested/output'):
            with self.subTest(path=path):
                self.assertFalse(path_boundaries.within(path, self.root))
                self.assertFalse(path.exists())
