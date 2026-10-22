"""Launch evidence binds original inputs without claiming action delivery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from otelc import node, node_provenance
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parent.parent


class NodeProvenanceTests(unittest.TestCase):
    def test_nonregular_inputs_cannot_block_before_regular_file_checks(self) -> None:
        with tempfile.TemporaryDirectory(prefix='otelc-input-fifo-', dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            fifo = root / 'configuration.json'
            os.mkfifo(fifo, 0o600)
            entry = root / 'app.cts'
            entry.write_text('console.log(7);\n')
            runtime = root / 'runtime'
            node.command(Policy.parse({'schema_version': 3}), runtime, 'typescript',
                         plain=True, backend='api', entry=entry, root=root)
            reader = ('import fs from "node:fs"; import { SourceInputs } from '
                + json.dumps(str(ROOT / 'adapters/node/src/source-inputs.ts')) + ';\n'
                'const plan = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));\n'
                'new SourceInputs(plan).config(process.argv[2]);\n')
            commands = [
                [sys.executable, '-c', 'import sys; from pathlib import Path; '
                 'from otelc.node_provenance import identity; identity(Path(sys.argv[1]))', str(fifo)],
                [node.executable(), '--input-type=module', '-e', reader,
                 str(runtime / 'node-plain-plan.json'), str(fifo)]]
            for command in commands:
                with self.subTest(reader=Path(command[0]).name):
                    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=2)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('not a regular file', result.stderr)

    def test_selected_node_alias_is_pinned_to_the_resolved_executable(self) -> None:
        selected = Path(node.executable()).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix='otelc-node-alias-') as temporary:
            alias = Path(temporary) / 'node'
            alias.symlink_to(selected)
            with patch.dict(os.environ, {'OTELC_NODE': str(alias)}):
                self.assertEqual(node.executable(), str(selected))

    def test_original_imports_and_extended_config_are_bound_for_both_compilers(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        for language, suffix, backend in [('javascript', 'cjs', 'native'),
                ('typescript', 'cts', 'native'), ('typescript', 'cts', 'api')]:
            with self.subTest(language=language, backend=backend), \
                    tempfile.TemporaryDirectory(prefix='otelc-inputs-', dir=ROOT / 'build') as temporary:
                directory = Path(temporary)
                application = directory / 'application'
                application.mkdir()
                entry = application / ('app.' + suffix)
                imported = application / ('dependency.' + suffix)
                annotation = ': number' if language == 'typescript' else ''
                imported.write_text('exports.workload = function workload(value' + annotation + ') { return value + 1; };\n')
                entry.write_text("console.log(require('./dependency." + suffix + "').workload(7));\n")
                base_config = directory / 'base.json'
                base_config.write_text('{"compilerOptions":{"target":"ES2022"}}\n')
                (application / 'tsconfig.json').write_text('{"extends":"../base.json"}\n')
                policy = directory / 'otelc.toml'
                policy.write_text('schema_version = 3\n[sources]\ninclude = ["**"]\n'
                    '[functions]\ninclude = ["*.workload"]\n[export]\n'
                    f'endpoint = "http://127.0.0.1:{capture.server.server_port}"\n')
                for plain in (True, False):
                    runtime = directory / ('plain' if plain else 'instrumented')
                    result = subprocess.run([sys.executable, '-m', 'otelc', '--config', str(policy), 'node',
                        '--language', language, '--backend', backend, '--root', str(application),
                        '--entry', str(entry), '--runtime', str(runtime), *(['--plain'] if plain else [])],
                        cwd=ROOT, capture_output=True, text=True, timeout=30)
                    self.assertEqual((result.returncode, result.stdout), (0, '8\n'), result.stderr)
                    prefix = 'node-plain' if plain else 'node'
                    manifest = json.loads((runtime / (prefix + '-launch.json')).read_text())
                    verified = json.loads((runtime / (prefix + '-inputs.json')).read_text())
                    self.assertEqual(verified['phase'], 'verified')
                    self.assertEqual(verified['launch_id'], manifest['launch_id'])
                    self.assertEqual(manifest['plan_sha256'], hashlib.sha256(
                        json.dumps(manifest['resolved_plan'], sort_keys=True).encode()).hexdigest())
                    self.assertEqual(manifest['resolved_plan']['backend'], backend)
                    self.assertFalse(verified['action_delivery_verified'])
                    self.assertEqual(manifest['entry']['sha256'], hashlib.sha256(entry.read_bytes()).hexdigest())
                    self.assertTrue(Path(manifest['node']['path']).is_absolute())
                    if plain and language == 'javascript':
                        self.assertEqual(verified['loaded_inputs'], [])
                        continue
                    records = verified['loaded_inputs']
                    sources = {item['path']: item for item in records if item['kind'] == 'source'}
                    self.assertEqual(set(sources), {str(entry), str(imported)})
                    self.assertEqual(sources[str(imported)]['sha256'], hashlib.sha256(imported.read_bytes()).hexdigest())
                    if language == 'typescript':
                        configs = {item['path'] for item in records if item['kind'] == 'compiler_config'}
                        self.assertLessEqual({str(base_config), str(application / 'tsconfig.json')}, configs)
                        stages = {item['kind'] for item in records}
                        self.assertIn('compiled', stages)
                    emissions = [item for item in records if item['kind'] in ('compiled', 'instrumented', 'loaded')]
                    self.assertTrue(emissions)
                    for item in emissions:
                        emitted = Path(item['output_file']).read_bytes()
                        self.assertEqual(hashlib.sha256(emitted).hexdigest(), item['sha256'])
                        self.assertEqual(item['format'], 'commonjs')
                        self.assertTrue(item['url'].startswith('file:'))
                    paths = {item['path'] for group in manifest['tooling'].values() for item in group}
                    self.assertIn(str(ROOT / 'package-lock.json'), paths)
                    self.assertIn(str(ROOT / 'adapters/node/src/compiler/typescript.mjs'), paths)
                    self.assertEqual(manifest['cwd'], str(ROOT))
                    if not plain:
                        self.assertNotEqual(verified['application_pid'], verified['sdk_pid'])

                    if language == 'typescript' and backend == 'api' and not plain:
                        self.check_negative_evidence(runtime, manifest, base_config, emissions)

    def check_negative_evidence(self, runtime: Path, manifest: dict, config: Path, emissions: list[dict]) -> None:
        cases = [(config, config.read_bytes() + b' '),
                 (Path(emissions[0]['output_file']), b'changed compiler output'),
                 (runtime / 'node-sources.jsonl', b'{truncated'),
                 (runtime / 'node-sources.jsonl', b'{"kind":"unknown"}\n'),
                 (runtime / 'node-sources.jsonl', b''),
                 (runtime / 'node-launch.json', b'{}')]
        for filename, replacement in cases:
            original = filename.read_bytes()
            try:
                filename.write_bytes(replacement)
                with self.subTest(filename=filename.name, replacement=replacement[:20]):
                    with self.assertRaises((ValueError, KeyError)):
                        node_provenance.verify(runtime, manifest)
            finally:
                filename.write_bytes(original)
        with self.assertRaises(FileNotFoundError):
            node_provenance.verify(runtime / 'missing', manifest)

    def test_mutating_an_original_loaded_file_invalidates_a_successful_launch(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='otelc-mutated-input-', dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            entry = root / 'app.cjs'
            entry.write_text("require('node:fs').appendFileSync(__filename, '// changed\\n');\n")
            policy = Policy.parse({'schema_version': 3, 'export': {
                'endpoint': f'http://127.0.0.1:{capture.server.server_port}'}})
            result = node.run(policy, root / 'runtime', 'javascript', entry=entry, root=root)
            self.assertEqual(result, 1)
            evidence = json.loads((root / 'runtime/node-inputs.json').read_text())
            self.assertEqual(evidence['phase'], 'failed')
            self.assertIn('changed', evidence['error'])

    def test_absent_config_creation_and_existing_application_failure_are_retained(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), \
                    tempfile.TemporaryDirectory(prefix='otelc-input-config-', dir=ROOT / 'build') as temporary:
                root = Path(temporary)
                entry = root / 'app.cts'
                entry.write_text("require('node:fs').writeFileSync(__dirname + '/tsconfig.json', '{}');\n"
                    f'process.exitCode = {exit_code};\n')
                policy = Policy.parse({'schema_version': 3, 'export': {
                    'endpoint': f'http://127.0.0.1:{capture.server.server_port}'}})
                result = node.run(policy, root / 'runtime', 'typescript', backend='api', entry=entry, root=root)
                self.assertEqual(result, exit_code or 1)
                report = json.loads((root / 'runtime/node-inputs.json').read_text())
                self.assertEqual(report['phase'], 'failed')
                self.assertIn('absent input changed', report['error'])

    def test_an_occupied_evidence_file_is_preserved_without_masking_application_failure(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        for plain in (True, False):
            for exit_code in (0, 7):
                with self.subTest(plain=plain, exit_code=exit_code), \
                        tempfile.TemporaryDirectory(prefix='otelc-input-occupied-', dir=ROOT / 'build') as temporary:
                    root = Path(temporary)
                    entry = root / 'app.cjs'
                    prefix = 'node-plain' if plain else 'node'
                    entry.write_text("require('node:fs').writeFileSync(process.argv[2] + '/"
                        + prefix + "-inputs.json', 'application-owned bytes');\n"
                        f'process.exitCode = {exit_code};\n')
                    runtime = root / 'runtime'
                    policy = Policy.parse({'schema_version': 3, 'export': {
                        'endpoint': f'http://127.0.0.1:{capture.server.server_port}'}})
                    result = node.run(policy, runtime, 'javascript', entry=entry, root=root,
                                      plain=plain, arguments=(str(runtime),))
                    self.assertEqual(result, exit_code or 1)
                    self.assertEqual((runtime / (prefix + '-inputs.json')).read_text(), 'application-owned bytes')

    def test_same_byte_config_symlink_substitution_keeps_its_distinct_origin(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='otelc-input-origin-', dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            for filename in ('first.json', 'second.json'):
                (root / filename).write_text('{"compilerOptions":{"target":"ES2022"}}\n')
            (root / 'tsconfig.json').symlink_to('first.json')
            entry = root / 'app.cts'
            entry.write_text("const fs = require('node:fs');\n"
                "fs.unlinkSync(__dirname + '/tsconfig.json');\n"
                "fs.symlinkSync('second.json', __dirname + '/tsconfig.json');\n")
            policy = Policy.parse({'schema_version': 3, 'export': {
                'endpoint': f'http://127.0.0.1:{capture.server.server_port}'}})
            runtime = root / 'runtime'
            self.assertEqual(node.run(policy, runtime, 'typescript', backend='api', entry=entry, root=root), 1)
            evidence = json.loads((runtime / 'node-inputs.json').read_text())
            self.assertEqual(evidence['phase'], 'failed')
            self.assertIn('input resolution changed', evidence['error'])

    def test_tooling_inventory_includes_links_and_refuses_missing_or_symlinked_subtrees(self) -> None:
        with tempfile.TemporaryDirectory(prefix='otelc-tooling-inputs-') as temporary:
            root = Path(temporary)
            source = root / 'source.js'
            source.write_text('original')
            link = root / 'linked.js'
            link.symlink_to(source.name)
            records = node_provenance.inventory(root)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]['symlink'], source.name)
            self.assertEqual(records[0]['sha256'], records[1]['sha256'])
            (root / 'linked-directory').symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlinked producer directories'):
                node_provenance.inventory(root)
            with self.assertRaisesRegex(ValueError, 'missing'):
                node_provenance.inventory(root / 'missing')
            failure = PermissionError('unreadable subtree')
            with patch('otelc.node_provenance.os.walk', side_effect=failure):
                with self.assertRaises(PermissionError):
                    node_provenance.inventory(root)
            with self.assertRaises(PermissionError):
                node_provenance.raise_walk_error(failure)
