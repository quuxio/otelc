"""Private Go backports keep honest build metadata and immutable caller bindings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci import build_go_upstream as tooling, go_scope_patch as backport
from otelc import go_build, go_prepared_tools as installed
import test_go_upstream_builder as fixtures

ROOT = Path(__file__).resolve().parents[1]


class GoScopeToolingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.GoUpstreamBuilderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.compiler.chmod(0o700)
        self.original = backport.ANCHOR + b'\treturn writeHookContext()\n}\n'
        target = self.fixture.module / backport.FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.original)
        result = self.original.replace(backport.ANCHOR, backport.ANCHOR + backport.GUARD)
        for name, value in (('BEFORE', backport.digest(self.original)), ('AFTER', backport.digest(result))):
            selected = patch.object(backport, name, value)
            selected.start()
            self.addCleanup(selected.stop)
        self.fixture.details = ('tool: go1.27.3\n\tpath\t' + tooling.PACKAGE
                                + '\n\tmod\t' + tooling.context.UPSTREAM_MODULE + '\t(devel)\t\n')
        self.fixture.version = 'otelc version ' + backport.VERSION

    def command(self, arguments: list[str], **options) -> bytes:
        if arguments[1] == 'build':
            self.fixture.commands.append((arguments, options['directory'], dict(options['env'])))
            self.assertEqual(options['directory'], self.fixture.output / 'source')
            self.assertIn('-mod=readonly', arguments)
            self.assertIn('-buildvcs=false', arguments)
            self.assertIn('-ldflags=-X=' + tooling.context.UPSTREAM_MODULE
                          + '/tool/util.Version=' + backport.VERSION, arguments)
            tool = self.fixture.output / 'bin/otelc'
            tool.write_bytes(b'private patched weaver')
            tool.chmod(0o700)
            return b''
        return self.fixture.command(arguments, **options)

    def build(self) -> dict:
        with patch.object(tooling, 'command', side_effect=self.command):
            return tooling.build('go', self.fixture.output, scope_safe=True)

    def query(self, arguments: list[str], *options) -> bytes:
        if arguments[1:3] == ['env', '-json']:
            return json.dumps(self.fixture.observed).encode()
        if arguments[1:3] == ['version', '-m']:
            return self.fixture.details.encode()
        self.assertEqual(arguments, [str(self.fixture.output / 'bin/otelc'), 'version'])
        return self.fixture.version.encode()

    def prepare(self) -> installed.PreparedTools:
        with patch.object(installed, 'query', side_effect=self.query):
            return installed.prepare(str(self.fixture.compiler), self.fixture.output)

    def test_installation_and_readonly_reuse_retain_backport_and_actual_devel_metadata(self) -> None:
        value = self.build()
        self.assertEqual(value['schema_version'], 2)
        self.assertEqual(value['patch_profile'], backport.PROFILE)
        self.assertEqual(value['version_output'], 'otelc version ' + backport.VERSION)
        self.assertIn('\t(devel)\t', value['build_metadata'])
        self.assertFalse(any(args[1] == 'install' for args, _, _ in self.fixture.commands))
        bound = self.prepare()
        self.assertEqual(bound.toolchain(), value)
        with patch.object(installed.subprocess, 'run', side_effect=AssertionError('no execution')):
            bound.verify(value)
        source = self.fixture.output / 'source' / backport.FILE
        source.write_bytes(source.read_bytes() + b'// changed\n')
        with self.assertRaisesRegex(ValueError, 'fixed profile'):
            bound.verify(value)

    def test_official_version_label_or_release_metadata_cannot_certify_a_private_build(self) -> None:
        self.fixture.version = 'otelc version ' + tooling.context.UPSTREAM_VERSION
        with self.assertRaisesRegex(ValueError, 'version'):
            self.build()
        self.assertFalse((self.fixture.output / 'toolchain.json').exists())

    def test_modified_source_manifest_cannot_replace_the_callers_held_binding(self) -> None:
        value = self.build()
        bound = self.prepare()
        manifest = self.fixture.output / backport.MANIFEST
        manifest.write_bytes(manifest.read_bytes() + b'\n')
        changed = value | {'source_manifest_sha256': backport.digest(manifest.read_bytes())}
        with self.assertRaisesRegex(ValueError, 'changed during'):
            bound.verify(changed)
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            bound.verify(value)

    def test_restored_source_bytes_do_not_reset_the_callers_held_file_facts(self) -> None:
        value = self.build()
        bound = self.prepare()
        target = self.fixture.output / 'source' / backport.FILE
        original = target.read_bytes()
        target.write_bytes(original + b'changed')
        target.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'changed during'):
            bound.verify(value)

    def test_unknown_patch_version_or_source_identity_refuses_before_executing_tools(self) -> None:
        value = self.build()
        manifest = self.fixture.output / 'toolchain.json'
        for key, wrong in (('patch_profile', 'other'), ('version_output', 'official'),
                           ('source_manifest_sha256', '0' * 64)):
            manifest.write_text(json.dumps(value | {key: wrong}))
            with patch.object(installed, 'query', side_effect=AssertionError('no execution')), \
                    self.subTest(key=key), self.assertRaises(ValueError):
                installed.prepare(str(self.fixture.compiler), self.fixture.output)

    def test_scope_safe_cli_is_explicit(self) -> None:
        with patch('sys.argv', ['builder', '--scope-safe']), \
                patch.object(tooling, 'build', return_value={}) as build, \
                patch('builtins.print'):
            tooling.main()
        self.assertIs(build.call_args.kwargs['scope_safe'], True)

    def test_backport_cannot_silently_emit_an_existing_public_application_recipe(self) -> None:
        with patch.object(subprocess, 'run', side_effect=AssertionError('no compiler')):
            with self.assertRaisesRegex(ValueError, 'separately versioned'):
                go_build.build_project(self.fixture.root / 'no-source', '.', None,
                                       self.fixture.root / 'no-output', {'schema_version': 2})
        self.assertFalse((self.fixture.root / 'no-output').exists())


class ActualGoScopeToolingTests(unittest.TestCase):
    def test_actual_backport_fixes_raw_globals_and_keeps_required_trampoline_declarations(self) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='go-scope-tooling-', dir=ROOT / 'build') as temporary:
            base = Path(temporary).resolve()
            output = base / 'installed'
            tools = tooling.build(os.environ.get('OTELC_GO', 'go'), output, scope_safe=True)
            bound = installed.prepare(tools['go'], output)
            source = base / 'test-source'
            shutil.copytree(output / 'source', source)
            # Upstream tests copy and rewrite their fixtures. Reproduce normal
            # checkout modes only in this disposable copy, never the installer.
            source.chmod(0o700)
            for path in source.rglob('*'):
                path.chmod(path.stat().st_mode | 0o200)
            fixture = ROOT / 'tests/fixtures/go_scope_globals_test.go'
            (source / 'tool/internal/instrument/quux_scope_test.go').write_bytes(fixture.read_bytes())
            target = source / backport.FILE
            patched = target.read_bytes()
            original = patched.replace(backport.ANCHOR + backport.GUARD, backport.ANCHOR, 1)
            self.assertEqual(backport.digest(original), backport.BEFORE)
            target.write_bytes(original)
            command = [tools['go'], 'test', '-mod=readonly', '-count=1', './tool/internal/instrument']
            env = tooling.compiler_environment(tools, output)
            before = subprocess.run(command + ['-run', '^TestQuuxRawOnlyGlobals$'], cwd=source,
                                    env=env, capture_output=True, timeout=180)
            self.assertNotEqual(before.returncode, 0, before.stdout + before.stderr)
            self.assertIn(b'raw-only instrumentation injected an unused HookContext', before.stdout)
            target.write_bytes(patched)
            after = subprocess.run(command, cwd=source, env=env, capture_output=True, timeout=180)
            self.assertEqual(after.returncode, 0, (after.stdout + after.stderr)[-8192:])
            bound.verify(tools)
