"""The official HTTP product is pinned and its exact preservation patch is checked."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ci import check_go_http as checker
from otelc import go_http_bundle as bundle


class GoHTTPBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.archive = self.root / 'module.zip'
        self.output = self.root / 'product'
        self.rows = {name: b'fixed' for name in ('LICENSE', 'go.mod', 'go.sum', 'handler.go')}
        self.rows['transport.go'] = b'SetStatus(codes.Error, err.Error())\n' * 3
        self.rows['transport_test.go'] = b'unused tests'
        self.rows['README.md'] = b'unused documentation'

    def zipped(self, rows: dict[str, bytes], *, prefix: str | None = None) -> str:
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for name, content in rows.items():
                archive.writestr((prefix if prefix is not None else bundle.MODULE + '@' + bundle.VERSION + '/') + name, content)
        return hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_exact_product_preserves_errors_and_only_removes_telemetry_formatting(self) -> None:
        sha = self.zipped(self.rows)
        identity = {'Path': bundle.MODULE, 'Version': bundle.VERSION, 'Sum': bundle.SUM,
                    'GoModSum': bundle.MOD_SUM, 'Zip': str(self.archive)}
        with patch.object(bundle, 'ZIP_SHA256', sha), patch.object(bundle.tooling, 'compiler_environment', return_value={}), \
                patch.object(bundle.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(identity).encode())):
            product = bundle.prepare({'go': '/pinned/go'}, self.output)
        self.assertEqual(product['patch'], 'three telemetry error descriptions empty; original error objects unchanged')
        self.assertEqual((self.output / 'transport.go').read_bytes(), b'SetStatus(codes.Error, "")\n' * 3)
        self.assertEqual((self.output / 'handler.go').read_bytes(), self.rows['handler.go'])
        self.assertEqual(set(product['files']), {'LICENSE', 'go.mod', 'go.sum', 'handler.go', 'transport.go'})
        self.assertFalse((self.output / 'transport_test.go').exists())
        for key in ('Path', 'Version', 'Sum', 'GoModSum'):
            with self.subTest(key=key), patch.object(bundle.tooling, 'compiler_environment', return_value={}), \
                    patch.object(bundle.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(identity | {key: 'changed'}).encode())):
                with self.assertRaisesRegex(ValueError, 'checksums'):
                    bundle.prepare({'go': '/pinned/go'}, self.root / ('bad-' + key))

    def test_drift_oversize_unsafe_root_and_incomplete_or_changed_patch_are_refused(self) -> None:
        self.zipped(self.rows)
        with self.assertRaisesRegex(ValueError, 'pinned identity'):
            bundle.sources(self.archive)
        with patch.object(bundle, 'MAX_BYTES', 1), self.assertRaises(ValueError):
            bundle.sources(self.archive)
        for rows, prefix in ((self.rows, 'other/'),
                (self.rows | {'../escape.go': b'bad'}, None),
                ({key: value for key, value in self.rows.items() if key != 'LICENSE'}, None),
                (self.rows | {'transport.go': b'changed'}, None)):
            sha = self.zipped(rows, prefix=prefix)
            with patch.object(bundle, 'ZIP_SHA256', sha), self.assertRaises(ValueError):
                bundle.sources(self.archive)
        self.assertFalse(self.output.exists())

    def test_component_coverage_requires_both_actual_nonempty_production_files(self) -> None:
        profile = self.root / 'coverage.out'
        prefix = 'io.quux.otelc/go-http/'
        profile.write_text('mode: atomic\n' + prefix + 'http.go:1.1,2.1 10 1\n' + prefix + 'dispatch.go:1.1,2.1 10 1\n')
        self.assertEqual(checker.coverage(profile), {'http.go': 100.0, 'dispatch.go': 100.0})
        for content in ('mode: atomic\n', 'mode: atomic\n' + prefix + 'http.go:1.1,2.1 10 1\n',
                'mode: atomic\n' + prefix + 'extra.go:1.1,2.1 10 1\n',
                'mode: atomic\n' + prefix + 'http.go:1.1,2.1 0 1\n' + prefix + 'dispatch.go:1.1,2.1 10 1\n',
                'mode: atomic\n' + prefix + 'http.go:1.1,2.1 10 0\n' + prefix + 'dispatch.go:1.1,2.1 10 1\n'):
            profile.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                checker.coverage(profile)

    def test_checker_uses_owned_private_products_and_keeps_failures_visible(self) -> None:
        # This orchestration test starts no compiler; actual Go race checks run
        # through the same maintained command in make go-check.
        project = self.root / 'project'
        (project / 'build').mkdir(parents=True)
        source = project / 'adapters/go-http'
        source.mkdir(parents=True)
        (source / 'go.mod').write_text('module fixed\n')
        (source / 'http.go').write_text('package fixed\n')
        (source / 'dispatch.go').write_text('package fixed\n')
        tools = {'go': '/pinned/bin/go', 'go_version': 'go1.27.3'}
        profile = project / 'build/go-http-coverage.out'

        def run(command: list[str], **options: object) -> subprocess.CompletedProcess:
            if command[1] == 'test':
                profile.write_text('mode: atomic\nio.quux.otelc/go-http/http.go:1.1,2.1 1 1\nio.quux.otelc/go-http/dispatch.go:1.1,2.1 1 1\n')
            return subprocess.CompletedProcess(command, 0)

        with patch.object(checker, 'ROOT', project), patch.object(checker.tooling, 'build', return_value=tools), \
                patch.object(checker.tooling, 'compiler_environment', return_value={}), \
                patch.object(checker.go_http_bundle, 'prepare', return_value={'pinned': True}), \
                patch.object(checker.subprocess, 'check_output', return_value=''), \
                patch.object(checker.subprocess, 'run', side_effect=run) as runner:
            self.assertEqual(checker.check()['component_statement_percent'], {'http.go': 100., 'dispatch.go': 100.})
            environment = runner.call_args.kwargs['env']
            self.assertEqual(environment['GOTMPDIR'], environment['TMPDIR'])
            self.assertFalse(Path(environment['GOTMPDIR']).exists())
            with patch.object(checker.subprocess, 'check_output', return_value='needs format'), self.assertRaisesRegex(ValueError, 'gofmt'):
                checker.check()
            with patch.object(checker.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'go')), self.assertRaises(subprocess.CalledProcessError):
                checker.check()
