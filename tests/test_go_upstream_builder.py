"""The compiler wrapper is pinned before any generated application can use it."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ci import build_go_upstream as tooling
from otelc import go_context_bundle as context


class GoUpstreamBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / 'output'
        self.compiler = self.root / 'go-root/bin/go'
        self.compiler.parent.mkdir(parents=True)
        self.compiler.write_bytes(b'fixed selected compiler')
        self.module = self.root / 'module'
        asset = self.module / 'tool/data/otelc-bundle.tgz'
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b'fixed bundle')
        self.observed = {'GOROOT': str(self.compiler.parents[1]), 'GOVERSION': 'go1.27.3',
                         'GOOS': 'linux', 'GOHOSTOS': 'linux', 'GOARCH': 'amd64', 'GOHOSTARCH': 'amd64'}
        self.download = {'Path': context.UPSTREAM_MODULE, 'Version': context.UPSTREAM_VERSION,
                         'Dir': str(self.module), 'Sum': context.MODULE_SUM, 'GoModSum': context.MODULE_GO_SUM}
        self.details = ('tool: go1.27.3\n\tpath\t' + tooling.PACKAGE + '\n\tmod\t'
                        + context.UPSTREAM_MODULE + '\tv1.0.1\t' + context.MODULE_SUM + '\n')
        self.version = 'otelc version v1.0.1'
        patch = mock.patch.object(context, 'BUNDLE_SHA256', context.digest(b'fixed bundle'))
        patch.start()
        self.addCleanup(patch.stop)
        self.commands = []

    def command(self, arguments: list[str], *, directory: Path, env: dict[str, str]) -> bytes:
        self.commands.append((arguments, directory, dict(env)))
        if arguments[1:3] == ['env', '-json']:
            return json.dumps(self.observed).encode()
        if arguments[1:3] == ['mod', 'download']:
            return json.dumps(self.download).encode()
        if arguments[1:3] == ['mod', 'verify']:
            return b'all modules verified\n'
        if arguments[1] == 'install':
            tool = self.output / 'bin/otelc'
            tool.parent.mkdir(exist_ok=True)
            tool.write_bytes(b'fixed wrapper')
            return b''
        if arguments[1:3] == ['version', '-m']:
            return self.details.encode()
        self.assertEqual(arguments, [str(self.output / 'bin/otelc'), 'version'])
        return self.version.encode()

    def test_pinned_native_compiler_module_checksums_asset_and_executable_are_bound(self) -> None:
        with mock.patch.object(tooling, 'command', side_effect=self.command):
            value = tooling.build('go', self.output)
        self.assertEqual(value['go_version'], 'go1.27.3')
        self.assertEqual(value['go'], str(self.compiler))
        self.assertEqual(value['go_sha256'], context.digest(self.compiler.read_bytes()))
        self.assertEqual(value['weaver_sha256'], context.digest(b'fixed wrapper'))
        self.assertEqual(value['module_sum'], context.MODULE_SUM)
        self.assertEqual(value['module_go_sum'], context.MODULE_GO_SUM)
        self.assertEqual(json.loads((self.output / 'toolchain.json').read_bytes()), value)
        self.assertIn('require go.opentelemetry.io/otelc v1.0.1', (self.output / 'verify/go.mod').read_text())
        self.assertTrue(any(args[1:3] == ['mod', 'verify'] for args, _, _ in self.commands))
        self.assertEqual([env['GOTOOLCHAIN'] for _, _, env in self.commands], ['go1.27.3', *(['local'] * (len(self.commands) - 1))])

    def test_wrong_compiler_platform_module_or_asset_refuses_before_install(self) -> None:
        for field, bad in (('GOVERSION', 'go1.27.2'), ('GOOS', 'darwin'), ('GOARCH', 'arm64')):
            old = dict(self.observed)
            self.observed[field] = bad
            with self.subTest(field=field), mock.patch.object(tooling, 'command', side_effect=self.command), \
                    self.assertRaisesRegex(ValueError, 'native Go1.27.3'):
                tooling.build('go', self.output)
            self.observed = old
        for field, bad in (('Path', 'other'), ('Version', 'v1.0.0'), ('Sum', 'wrong'), ('GoModSum', 'wrong')):
            old = dict(self.download)
            self.download[field] = bad
            with self.subTest(field=field), mock.patch.object(tooling, 'command', side_effect=self.command), \
                    self.assertRaisesRegex(ValueError, 'pinned release'):
                tooling.build('go', self.output)
            self.download = old
        (self.module / 'tool/data/otelc-bundle.tgz').write_bytes(b'changed')
        with mock.patch.object(tooling, 'command', side_effect=self.command), self.assertRaisesRegex(ValueError, 'asset'):
            tooling.build('go', self.output)
        self.assertFalse(any(args[1] == 'install' for args, _, _ in self.commands))

    def test_wrong_tool_module_identity_and_reported_version_refuse(self) -> None:
        original = self.details
        for details in (original.replace(context.MODULE_SUM, 'other'), original.replace(tooling.PACKAGE, 'other')):
            self.details = details
            with mock.patch.object(tooling, 'command', side_effect=self.command), self.assertRaisesRegex(ValueError, 'metadata'):
                tooling.build('go', self.output)
        self.details = original
        self.version = 'otelc version v1.0.0'
        with mock.patch.object(tooling, 'command', side_effect=self.command), self.assertRaisesRegex(ValueError, 'version'):
            tooling.build('go', self.output)
        self.assertFalse((self.output / 'toolchain.json').exists())

    def test_public_build_environment_keeps_owned_caches_and_removes_ambient_overrides(self) -> None:
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'private', 'AWS_SECRET_ACCESS_KEY': 'private',
                'OTELC_RULES': 'foreign', 'GOFLAGS': '-overlay=foreign', 'GOWORK': 'foreign',
                'GOSUMDB': 'off', 'PATH': '/bin'}, clear=True):
            env = tooling.environment(self.output)
        self.assertFalse(any(key.startswith(('GH_', 'AWS_', 'OTEL')) for key in env))
        self.assertEqual((env['GOFLAGS'], env['GOWORK'], env['GOSUMDB']), ('', 'off', 'sum.golang.org'))
        self.assertEqual(env['GOPATH'], str(tooling.ROOT / 'build/go-path'))
        self.assertEqual(env['GOMODCACHE'], str(tooling.ROOT / 'build/go-module-cache'))
        self.assertEqual(env['GOBIN'], str(self.output / 'bin'))

    def test_subprocess_failure_has_a_retained_usable_diagnostic(self) -> None:
        result = subprocess.CompletedProcess(['go'], 7, b'', b'compiler failed\n')
        with mock.patch.object(tooling.subprocess, 'run', return_value=result), \
                self.assertRaisesRegex(RuntimeError, 'exit 7.*compiler failed'):
            tooling.command(['go', 'version'], directory=self.root, env={})
        self.assertEqual((self.root / 'go-tool-error.log').read_bytes(), b'compiler failed\n')
        with mock.patch.object(tooling.subprocess, 'run', return_value=subprocess.CompletedProcess(['go'], 0, b'good', b'')):
            self.assertEqual(tooling.command(['go'], directory=self.root, env={}), b'good')

    def test_cli_uses_explicit_operator_arguments(self) -> None:
        with mock.patch('sys.argv', ['builder', '--go', '/selected/go', '--output', str(self.output)]), \
                mock.patch.object(tooling, 'build', return_value={'checked': True}) as build, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            tooling.main()
        build.assert_called_once_with('/selected/go', self.output)
        self.assertEqual(json.loads(output.getvalue()), {'checked': True})
