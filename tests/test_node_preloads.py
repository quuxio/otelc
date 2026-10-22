"""Preload metadata uses actual Node resolution without running application code."""

from __future__ import annotations

import json
import contextlib
import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from otelc import node, node_preloads, node_provenance
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parent.parent


class NodePreloadTests(unittest.TestCase):
    def test_relative_quoted_preloads_conditions_and_actual_execution_order(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            audit = root / 'audit.txt'
            for name, marker in (('one preload.cjs', 'one'), ('two.cjs', 'two'), ('three preload.mjs', 'three')):
                prefix = "import fs from 'node:fs';" if name.endswith('mjs') else "const fs = require('node:fs');"
                (root / name).write_text(prefix + '\nfs.appendFileSync(' + json.dumps(str(audit)) + ',' + json.dumps(marker + '\n') + ');\n')
            package = root / 'node_modules/preload-package'
            package.mkdir(parents=True)
            (package / 'package.json').write_text(json.dumps({'exports': {'otelc-test': './chosen.cjs', 'default': './other.cjs'}}))
            (package / 'chosen.cjs').write_text("require('node:fs').appendFileSync(" + json.dumps(str(audit)) + ", 'chosen\\n');\n")
            (package / 'other.cjs').write_text("throw new Error('wrong package condition');\n")
            path_package = root / 'module-path/path-preload'
            path_package.mkdir(parents=True)
            (path_package / 'package.json').write_text('{"main":"index.cjs"}\n')
            (path_package / 'index.cjs').write_text("require('node:fs').appendFileSync(" + json.dumps(str(audit)) + ", 'from-path\\n');\n")
            entry = root / 'app.cjs'
            entry.write_text("console.log(require('node:fs').readFileSync(" + json.dumps(str(audit)) + ", 'utf8'));\n")
            options = ('--title="literal --require=ignored" --require="./one preload.cjs" '
                       '-r ./two.cjs --import="./three preload.mjs" --require=preload-package '
                       '--require=node:fs --conditions=otelc-test --require=path-preload '
                       '--require="./one preload.cjs" --import="./three preload.mjs"')
            policy = Policy.parse({'schema_version': 3, 'export': {'endpoint': f'http://127.0.0.1:{capture.server.server_port}'}})
            for plain in (True, False):
                with self.subTest(plain=plain):
                    audit.unlink(missing_ok=True)
                    runtime = root / ('plain' if plain else 'instrumented')
                    with patch.dict(os.environ, {'NODE_OPTIONS': options, 'NODE_PATH': './module-path'}), contextlib.chdir(root):
                        invocation = node.command(policy, runtime, 'javascript', root=root, entry=entry, plain=plain)
                        self.assertFalse(audit.exists(), 'planning executed an application preload')
                        prefix = 'node-plain' if plain else 'node'
                        manifest = json.loads((runtime / (prefix + '-launch.json')).read_text())
                        metadata = manifest['preloads']
                        self.assertEqual([item['specifier'] for item in metadata['entries']],
                            ['./one preload.cjs', './two.cjs', 'preload-package', 'node:fs', 'path-preload',
                             './one preload.cjs', './three preload.mjs', './three preload.mjs'])
                        files = {Path(item['file']['path']).name for item in metadata['entries'] if item['kind'] == 'file'}
                        self.assertEqual(files, {'one preload.cjs', 'two.cjs', 'chosen.cjs', 'index.cjs', 'three preload.mjs'})
                        self.assertEqual(metadata['resolution_environment']['NODE_PATH']['value'], './module-path')
                        node_preloads.verify(manifest['request'], metadata)
                        result = subprocess.run(invocation, cwd=root, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(audit.read_text().splitlines(), ['one', 'two', 'chosen', 'from-path', 'three'])
                    self.assertEqual(result.stdout, 'one\ntwo\nchosen\nfrom-path\nthree\n\n')
                    if not plain:
                        self.assertEqual(node.wait_report(runtime)['phase'], 'closed')
                    self.assertTrue(node_provenance.finish(runtime, manifest))
                    for altered in ('order', 'kind', 'version', 'argv', 'coverage'):
                        changed = copy.deepcopy(metadata)
                        if altered == 'order':
                            changed['entries'].reverse()
                        elif altered == 'kind':
                            changed['entries'][0]['kind'] = 'unknown'
                        elif altered == 'version':
                            changed['schema_version'] = True
                        elif altered == 'argv':
                            changed['argv'] = ['unrequested-tool']
                        else:
                            changed['coverage'] = 'all_dependencies'
                        with self.subTest(altered=altered), self.assertRaises(ValueError):
                            node_preloads.verify(manifest['request'], changed)

    def test_changed_or_missing_preload_and_same_byte_alias_changes_are_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            first = root / 'first.cjs'
            second = root / 'second.cjs'
            first.write_text('module.exports = 7;\n')
            second.write_bytes(first.read_bytes())
            alias = root / 'alias.cjs'
            alias.symlink_to(first.name)
            with patch.dict(os.environ, {'NODE_OPTIONS': '--require=./alias.cjs'}), contextlib.chdir(root):
                runtime = root / 'runtime'
                node.command(Policy.parse({'schema_version': 3}), runtime, 'javascript', plain=True)
            manifest = json.loads((runtime / 'node-plain-launch.json').read_text())
            node_preloads.verify(manifest['request'], manifest['preloads'])
            alias.unlink()
            alias.symlink_to(second.name)
            with self.assertRaisesRegex(ValueError, 'preload'):
                node_preloads.verify(manifest['request'], manifest['preloads'])
            alias.unlink()
            alias.symlink_to(first.name)
            first.write_text('module.exports = 8;\n')
            with self.assertRaisesRegex(ValueError, 'preload'):
                node_preloads.verify(manifest['request'], manifest['preloads'])
            first.unlink()
            with self.assertRaises((ValueError, FileNotFoundError)):
                node_preloads.verify(manifest['request'], manifest['preloads'])

    def test_node_option_quoting_keeps_tabs_and_single_quotes_literal(self) -> None:
        self.assertEqual(node_preloads.tokens('--require "a b" --title="x\\\"y"'),
                         ['--require', 'a b', '--title=x"y'])
        self.assertEqual(node_preloads.tokens("--require 'a'\t"), ['--require', "'a'\t"])
        for text in ('--require "unfinished', '--require "bad\\'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                node_preloads.tokens(text)

    def test_missing_preload_is_refused_without_running_an_earlier_preload(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            audit = root / 'audit'
            (root / 'first.cjs').write_text("require('node:fs').writeFileSync(" + json.dumps(str(audit)) + ", 'ran');\n")
            with patch.dict(os.environ, {'NODE_OPTIONS': '--require=./first.cjs --require=./missing.cjs'}), contextlib.chdir(root):
                with self.assertRaisesRegex(ValueError, '^Node preload resolution failed$'):
                    node.command(Policy.parse({'schema_version': 3}), root / 'runtime', 'javascript', plain=True)
            self.assertFalse(audit.exists())
