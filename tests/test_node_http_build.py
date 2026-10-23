"""The private HTTP product must bind exact upstream bytes and preserve a good cache."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci.build_node_http import ROOT, build, replace_once


class NodeHttpBuildTests(unittest.TestCase):
    def fixture(self, directory: str) -> Path:
        root = Path(directory).resolve()
        package = root / 'node_modules/@opentelemetry/instrumentation-http'
        shutil.copytree(ROOT / 'node_modules/@opentelemetry/instrumentation-http', package)
        helper = root / 'adapters/node/src/http-preservation.ts'
        helper.parent.mkdir(parents=True)
        helper.write_bytes((ROOT / 'adapters/node/src/http-preservation.ts').read_bytes())
        return root

    def test_build_is_repeatable_preserves_upstream_and_refuses_changed_cached_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            source = root / 'node_modules/@opentelemetry/instrumentation-http/build/src/http.js'
            before = source.read_bytes()
            with patch('ci.build_node_http.ROOT', root):
                product = build()
                manifest = (root / 'build/tools/otelc-http.json').read_bytes()
                self.assertEqual(build(), product)
                self.assertEqual((root / 'build/tools/otelc-http.json').read_bytes(), manifest)
                self.assertEqual(source.read_bytes(), before)
                (product / 'build/src/http.js').write_bytes(b'changed cached runtime')
                with self.assertRaisesRegex(ValueError, 'product bytes changed'):
                    build()
                self.assertEqual((root / 'build/tools/otelc-http.json').read_bytes(), manifest)

    def test_changed_upstream_version_or_source_cannot_replace_a_qualified_product(self) -> None:
        for change in ('version', 'source'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = self.fixture(temporary)
                package = root / 'node_modules/@opentelemetry/instrumentation-http'
                with patch('ci.build_node_http.ROOT', root):
                    build()
                    manifest = (root / 'build/tools/otelc-http.json').read_bytes()
                    if change == 'version':
                        data = json.loads((package / 'package.json').read_text())
                        data['version'] = '0.224.0'
                        (package / 'package.json').write_text(json.dumps(data))
                    else:
                        (package / 'build/src/http.js').write_bytes(b'new source without version change')
                    with self.assertRaisesRegex(ValueError, 'qualified 0.223|checksum mismatch'):
                        build()
                    self.assertEqual((root / 'build/tools/otelc-http.json').read_bytes(), manifest)

    def test_cache_file_set_and_exact_patch_context_cannot_be_approximated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            with patch('ci.build_node_http.ROOT', root):
                product = build()
                (product / 'extra.js').write_text('extra runtime')
                with self.assertRaisesRegex(ValueError, 'file set changed'):
                    build()
        for source in ('missing', 'needle needle'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'context changed'):
                replace_once(source, 'needle', 'replacement')
