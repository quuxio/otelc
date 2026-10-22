"""A fixed source launch recipe is validated against original caller intent."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from otelc import node, node_provenance, node_recipe
from otelc.config import Policy

ROOT = Path(__file__).resolve().parent.parent


class NodeRecipeTests(unittest.TestCase):
    def test_fixed_recipe_binds_policy_paths_arguments_and_exact_bootstrap(self) -> None:
        for language, plain in (('javascript', True), ('javascript', False), ('typescript', True), ('typescript', False)):
            with self.subTest(language=language, plain=plain), tempfile.TemporaryDirectory(dir=ROOT / 'build') as temporary:
                root = Path(temporary)
                entry = root / ('app.cts' if language == 'typescript' else 'app.cjs')
                entry.write_text('console.log(process.argv.slice(2));\n')
                runtime = root / 'runtime'
                policy = Policy.parse({'schema_version': 3, 'metrics': {'collection': False}})
                argv = node.command(policy, runtime, language, entry=entry, root=root, plain=plain,
                                    backend='api', arguments=('a b', '--literal'))
                prefix = 'node-plain' if plain else 'node'
                request = json.loads((runtime / (prefix + '-request.json')).read_text())
                manifest = json.loads((runtime / (prefix + '-launch.json')).read_text())
                self.assertEqual(request['arguments'], ['a b', '--literal'])
                self.assertEqual(request['policy'], policy.data)
                self.assertEqual(manifest['argv'], argv)
                node_recipe.verify(runtime, request, manifest, producer=ROOT)
                changed_request = copy.deepcopy(request)
                changed_request['schema_version'] = True
                with self.assertRaisesRegex(ValueError, 'invalid Node source launch request'):
                    node_recipe.verify(runtime, changed_request, manifest, producer=ROOT)
                for changed_field, replacement in (('argv', [*argv, 'unrequested']),
                                                    ('cwd', '/different/cwd'),
                                                    ('backend', 'native')):
                    changed = copy.deepcopy(manifest)
                    changed[changed_field] = replacement
                    with self.subTest(changed_field=changed_field), self.assertRaises(ValueError):
                        node_recipe.verify(runtime, request, changed, producer=ROOT)
                if plain and language == 'javascript':
                    continue
                bootstrap = runtime / (prefix + '-bootstrap.cjs')
                original = bootstrap.read_bytes()
                bootstrap.write_bytes(original + b'// not part of the fixed recipe\n')
                changed = copy.deepcopy(manifest)
                changed['generated'] = [node_provenance.identity(Path(item['path'])) for item in changed['generated']]
                with self.assertRaisesRegex(ValueError, 'bootstrap'):
                    node_recipe.verify(runtime, request, changed, producer=ROOT)

    def test_rehashed_plan_cannot_change_the_requested_metric_or_source_selection(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as temporary:
            runtime = Path(temporary) / 'runtime'
            node.command(Policy.parse({'schema_version': 3}), runtime, 'typescript', plain=True, backend='api')
            manifest = json.loads((runtime / 'node-plain-launch.json').read_text())
            request = json.loads((runtime / 'node-plain-request.json').read_text())
            changed = copy.deepcopy(manifest)
            changed['resolved_plan']['metrics']['collection'] = False
            changed['resolved_plan']['source_matchers']['include'] = ['**']
            (runtime / 'node-plain-plan.json').write_text(node_recipe.encoded(changed['resolved_plan']))
            changed['plan_sha256'] = hashlib.sha256(json.dumps(changed['resolved_plan'], sort_keys=True).encode()).hexdigest()
            changed['generated'] = [node_provenance.identity(Path(item['path'])) for item in changed['generated']]
            with self.assertRaisesRegex(ValueError, 'plan'):
                node_recipe.verify(runtime, request, changed, producer=ROOT)

    def test_environment_distinguishes_absent_empty_and_changed_values_without_tooling_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            cache = root / 'application-cache'
            with patch.dict(os.environ, {'NODE_COMPILE_CACHE': str(cache), 'OTEL_SDK_DISABLED': 'true'}):
                for value in (None, ''):
                    with self.subTest(value=value), patch.dict(os.environ):
                        if value is None:
                            os.environ.pop('NODE_OPTIONS', None)
                        else:
                            os.environ['NODE_OPTIONS'] = value
                        runtime = root / ('absent' if value is None else 'empty')
                        node.command(Policy.parse({'schema_version': 3}), runtime, 'javascript', plain=True)
                        manifest = json.loads((runtime / 'node-plain-launch.json').read_text())
                        request = json.loads((runtime / 'node-plain-request.json').read_text())
                        record = request['environment']['NODE_OPTIONS']
                        self.assertEqual(record, {'state': 'absent'} if value is None else {
                            'state': 'present', 'value': '', 'sha256': hashlib.sha256(b'').hexdigest()})
                        self.assertEqual(manifest['tooling_environment']['NODE_COMPILE_CACHE'], {'state': 'absent'})
                        self.assertNotIn('OTEL_SDK_DISABLED', manifest['tooling_environment'])
                        self.assertFalse(cache.exists(), 'inspection applied application compile-cache settings')
                        changed = copy.deepcopy(request)
                        changed['environment']['NODE_OPTIONS'] = {'state': 'present', 'value': '', 'sha256': '0' * 64}
                        with self.assertRaisesRegex(ValueError, 'value/hash'):
                            node_recipe.verify(runtime, changed, manifest, producer=ROOT)
                        changed = copy.deepcopy(manifest)
                        changed['tooling_environment']['NODE_OPTIONS'] = node_recipe.environment_record('--no-warnings')
                        with self.assertRaisesRegex(ValueError, 'metadata environment'):
                            node_recipe.verify(runtime, request, changed, producer=ROOT)

    def test_original_toml_bytes_and_policy_resolution_are_bound_before_application_launch(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            configuration = root / 'otelc.toml'
            text = 'schema_version = 3\n[metrics]\ncollection = false\n'
            configuration.write_text(text)
            policy = Policy.load(configuration)
            runtime = root / 'runtime'
            node.command(policy, runtime, 'javascript', plain=True)
            manifest = json.loads((runtime / 'node-plain-launch.json').read_text())
            original = json.loads((runtime / 'node-plain-request.json').read_text())
            self.assertEqual(original['policy_source']['text'], text)
            self.assertEqual(original['policy_source']['sha256'], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(original['policy']['metrics']['collection'], False)
            configuration.write_text(text + '# changed\n')
            with self.assertRaisesRegex(ValueError, 'policy source changed'):
                node_recipe.verify(runtime, original, manifest, producer=ROOT)
            with self.assertRaisesRegex(ValueError, 'policy source changed'):
                node.command(policy, root / 'changed-source', 'javascript', plain=True)

    def test_changed_generated_plan_is_refused_before_application_entry(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            entry = root / 'app.cjs'
            entry.write_text("console.log('application entry ran');\n")
            runtime = root / 'runtime'
            argv = node.command(Policy.parse({'schema_version': 3}), runtime, 'javascript', root=root, entry=entry)
            filename = runtime / 'node-plan.json'
            plan = json.loads(filename.read_text())
            plan['metrics']['collection'] = False
            filename.write_text(node_recipe.encoded(plan))
            result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertIn('source plan differs from the retained launch manifest', result.stderr)
            self.assertFalse((runtime / 'node-status.json').exists())
