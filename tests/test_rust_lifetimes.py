"""Literal discovery/selector refusals; these do not execute the Rust compiler."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from otelc import rust_lifetimes as lifetimes
from otelc.config import Policy


class RustLifetimeSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'application'; self.root.mkdir()
        self.app = self.root / 'app.rs'; self.app.write_text('mod model; fn main(){} const fn constant(){}\n')
        self.model = self.root / 'model.rs'
        self.model.write_text('pub struct Item(i32); pub struct Generic<T>(T); pub struct CopyItem(i32);\n')
        self.sources = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (self.app, self.model)}
        self.path = self.base / 'inventory.json'
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['rust'],
            'sources': {'include': ['**']}, 'lifetimes': {'enabled': True, 'include': ['*']}})
        self.document = self.fixture()

    def declaration(self, path: Path, name: str) -> dict:
        return {'identity': path.relative_to(self.root).as_posix() + '::' + name,
            'file': str(path), 'compiler_name': name, 'start': 0, 'end': len(path.read_bytes()),
            'asynchronous': False, 'expanded': False}

    def fixture(self) -> dict:
        rows = []
        for index in range(5):
            generic, copied = index == 3, index == 4
            name = 'model::Generic' if generic else 'model::CopyItem' if copied else 'model::Item'
            rows.append({'local': index, 'role': 'return' if index == 0 else 'argument' if index == 1 else 'local',
                'source': self.declaration(self.model, name), 'compiler_type': name,
                'rendered_type': name + ('<T>' if generic else ''), 'kind': 'struct', 'needs_drop': not copied,
                'size': None if generic else 4, 'alignment': None if generic else 4,
                'layout_error': 'TooGeneric(T)' if generic else None})
        sites = [{'kind': kind, 'block': 0, 'statement': None if kind == 'drop' else 0,
            'local': 4 if kind == 'copy' else 2, 'other': 5, 'projected': False,
            'cleanup': kind == 'drop', 'unwind': 'Continue' if kind == 'drop' else None}
            for kind in sorted(lifetimes.SITE_KINDS)]
        return {'schema_version': 1, 'kind': 'otelc.rust.lifetime-inventory', 'sources': self.sources,
            'target': 'literal-target', 'pointer_width': 64, 'source_complete': False, 'instrumentation_complete': False,
            'bodies': [{'function': self.declaration(self.app, 'main'), 'phase': 'Runtime(PostCleanup)',
                'local_count': 6, 'block_count': 1, 'argument_count': 1, 'locals': rows, 'sites': sites}],
            'skipped': [{'function': self.declaration(self.app, 'constant'), 'reason': 'const'}]}

    def read(self, value: dict | None = None, policy: Policy | None = None) -> dict:
        self.path.write_text(json.dumps(self.document if value is None else value))
        return lifetimes.read(self.path, policy or self.policy, self.root, self.sources)

    def version_two(self) -> dict:
        value = copy.deepcopy(self.document)
        value['schema_version'] = 2
        value['bodies'][0]['unavailable_locals'] = [{'local': 5, 'role': 'local',
            'compiler_type': 'std::fs::File', 'rendered_type': 'File',
            'kind': 'foreign_adt', 'needs_drop': True}]
        return value

    def test_unavailable_selected_types_remain_visible_and_cannot_be_woven(self) -> None:
        value = self.version_two()
        selected = self.read(value)
        self.assertEqual(selected['schema_version'], 2)
        self.assertEqual(selected['bodies'][0]['unavailable_locals'], value['bodies'][0]['unavailable_locals'])
        self.assertFalse(selected['source_complete'])
        for resolve in (lifetimes.weave_bindings, lifetimes.weave_request):
            with self.subTest(resolve=resolve.__name__), self.assertRaisesRegex(ValueError, 'unavailable.*File'):
                resolve(self.path, self.policy, self.root, self.sources)
        data = copy.deepcopy(self.policy.data)
        data['lifetimes']['exclude'] = ['*File*']
        other = self.read(value, Policy.parse(data))
        self.assertEqual(other['bodies'][0]['unavailable_locals'], [])
        self.assertTrue(lifetimes.weave_request(self.path, Policy.parse(data), self.root, self.sources)['lifetimes'])

    def test_unavailable_only_selection_is_not_silently_empty(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['lifetimes']['include'] = ['*File*']
        policy = Policy.parse(data)
        selected = self.read(self.version_two(), policy)
        self.assertEqual(len(selected['bodies']), 1)
        self.assertEqual(selected['bodies'][0]['locals'], [])
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            lifetimes.weave_request(self.path, policy, self.root, self.sources)
        data['lifetimes']['enabled'] = False
        self.assertEqual(self.read(self.version_two(), Policy.parse(data))['bodies'], [])

    def test_full_body_fingerprint_includes_excluded_unavailable_types(self) -> None:
        value = self.version_two()
        data = copy.deepcopy(self.policy.data)
        data['lifetimes']['exclude'] = ['*File*']
        policy = Policy.parse(data)
        self.read(value, policy)
        first = lifetimes.weave_request(self.path, policy, self.root, self.sources)
        value['bodies'][0]['unavailable_locals'][0]['needs_drop'] = False
        self.read(value, policy)
        second = lifetimes.weave_request(self.path, policy, self.root, self.sources)
        self.assertEqual(first['lifetimes'], second['lifetimes'])
        self.assertNotEqual(first['lifetime_bodies'], second['lifetime_bodies'])

    def test_unavailable_return_argument_and_local_roles_remain_distinct(self) -> None:
        value = self.version_two()
        template = value['bodies'][0]['unavailable_locals'][0]
        value['bodies'][0].update(locals=[], sites=[], unavailable_locals=[
            template | {'local': index, 'role': role}
            for index, role in ((0, 'return'), (1, 'argument'), (5, 'local'))])
        selected = self.read(value)
        self.assertEqual([row['role'] for row in selected['bodies'][0]['unavailable_locals']],
                         ['return', 'argument', 'local'])

    def test_unavailable_metadata_is_closed_typed_unique_even_when_excluded(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['lifetimes']['exclude'] = ['*']
        for key, bad in (('local', True), ('local', 6), ('local', 2), ('role', 'argument'),
                         ('compiler_type', ''), ('compiler_type', 1), ('rendered_type', ''),
                         ('kind', 'unknown'), ('needs_drop', 1), ('extra', 'field')):
            value = self.version_two()
            value['bodies'][0]['unavailable_locals'][0][key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.read(value, Policy.parse(data))
        for rows in (None, {}, [None], self.version_two()['bodies'][0]['unavailable_locals'] * 2):
            value = self.version_two(); value['bodies'][0]['unavailable_locals'] = rows
            with self.subTest(rows=rows), self.assertRaises(ValueError): self.read(value)
        value = self.version_two(); del value['bodies'][0]['unavailable_locals']
        self.refuses(value)
        value = self.version_two(); value['schema_version'] = 1
        self.refuses(value)

    def refuses(self, document: dict) -> None:
        self.path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            lifetimes.read(self.path, self.policy, self.root, self.sources)

    def test_selection_retains_types_sites_layout_gaps_and_explicit_incomplete_status(self) -> None:
        original = copy.deepcopy(self.document)
        result = self.read()
        self.assertEqual(result['bodies'], self.document['bodies'])
        self.assertFalse(result['source_complete']); self.assertFalse(result['instrumentation_complete'])
        self.assertEqual(len(result['selection_sha256']), 64)
        self.assertEqual(self.document, original)
        changed = copy.deepcopy(self.policy.data); changed['metrics'].update(collection=False, emission=False)
        self.assertEqual(self.read(policy=Policy.parse(changed)), result)
        value = copy.deepcopy(self.document); value['bodies'][0]['locals'][2].update(size=0, alignment=1)
        self.assertEqual(self.read(value)['bodies'][0]['locals'][2]['size'], 0)

    def test_common_source_and_lifetime_exclusions_take_precedence_and_off_selects_nothing(self) -> None:
        for changes in ({'lifetimes': {'enabled': False}},
                        {'lifetimes': {'enabled': True, 'include': ['*'], 'exclude': ['*']}},
                        {'sources': {'include': ['**'], 'exclude': ['model.rs']}},
                        {'sources': {'include': ['**'], 'exclude': ['app.rs']}}):
            data = copy.deepcopy(self.policy.data); data.update(changes)
            self.assertEqual(self.read(policy=Policy.parse(data))['bodies'], [])
        data = copy.deepcopy(self.policy.data)
        data['lifetimes'].update(include=['*'], exclude=['*Copy*', '*Generic*'])
        selected = self.read(policy=Policy.parse(data))['bodies'][0]
        self.assertEqual([row['local'] for row in selected['locals']], [0, 1, 2])
        self.assertFalse(any(row['kind'] == 'copy' for row in selected['sites']))

    def test_private_weave_bindings_keep_every_selected_local_and_recheck_original_inputs(self) -> None:
        self.read()
        bindings = lifetimes.weave_bindings(self.path, self.policy, self.root, self.sources)
        self.assertEqual(bindings, {'app.rs::main': {'0': 'model::Item', '1': 'model::Item',
            '2': 'model::Item', '3': 'model::Generic', '4': 'model::CopyItem'}})
        disabled = copy.deepcopy(self.policy.data)
        disabled['lifetimes']['enabled'] = False
        self.assertEqual(lifetimes.weave_bindings(self.path, Policy.parse(disabled), self.root, self.sources), {})
        self.model.write_text('changed source\n')
        with self.assertRaisesRegex(ValueError, 'original source changed'):
            lifetimes.weave_bindings(self.path, self.policy, self.root, self.sources)

    def test_weave_request_binds_complete_body_even_when_policy_selects_a_subset(self) -> None:
        self.read()
        data = copy.deepcopy(self.policy.data)
        data['lifetimes'].update(include=['*CopyItem'])
        requested = lifetimes.weave_request(self.path, Policy.parse(data), self.root, self.sources)
        self.assertEqual(requested['lifetimes'], {'app.rs::main': {'4': 'model::CopyItem'}})
        raw = json.dumps(self.document['bodies'][0], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        self.assertEqual(requested['lifetime_bodies'], {'app.rs::main': hashlib.sha256(raw).hexdigest()})
        changed = copy.deepcopy(self.document)
        changed['bodies'][0]['block_count'] += 1
        self.read(changed)
        other = lifetimes.weave_request(self.path, Policy.parse(data), self.root, self.sources)
        self.assertEqual(other['lifetimes'], requested['lifetimes'])
        self.assertNotEqual(other['lifetime_bodies'], requested['lifetime_bodies'])

    def test_unknown_missing_wrongly_typed_header_and_completion_claims_refuse(self) -> None:
        for key, bad in [('schema_version', True), ('schema_version', 1.0), ('kind', 'foreign'),
                         ('sources', {}), ('target', None), ('target', ''), ('pointer_width', 32),
                         ('source_complete', 0), ('instrumentation_complete', True),
                         ('bodies', {}), ('skipped', None)]:
            value = copy.deepcopy(self.document); value[key] = bad
            with self.subTest(key=key, bad=bad): self.refuses(value)
        for key in self.document:
            value = copy.deepcopy(self.document); del value[key]
            with self.subTest(missing=key): self.refuses(value)
        self.refuses(self.document | {'unknown': 0})

    def test_malformed_excluded_source_rows_and_duplicate_bodies_still_refuse(self) -> None:
        for key, bad in [('identity', 'forged'), ('file', str(self.base / 'foreign.rs')),
                         ('compiler_name', ''), ('start', True), ('end', 1.0), ('end', 0),
                         ('asynchronous', 0), ('expanded', None)]:
            value = copy.deepcopy(self.document); value['bodies'][0]['function'][key] = bad
            with self.subTest(key=key): self.refuses(value)
        value = copy.deepcopy(self.document); value['bodies'].append(copy.deepcopy(value['bodies'][0]))
        self.refuses(value)
        value = copy.deepcopy(self.document); value['bodies'][0]['function']['asynchronous'] = True
        self.refuses(value)
        value = copy.deepcopy(self.document); value['bodies'][0]['locals'][0]['source'] = None
        self.refuses(value)

    def test_invalid_layouts_roles_local_numbers_and_duplicate_locals_refuse(self) -> None:
        for key, bad in [('local', True), ('local', 6), ('role', 'argument'), ('kind', 'primitive'),
                         ('needs_drop', 1), ('rendered_type', ''), ('compiler_type', 'foreign'),
                         ('size', -1), ('alignment', 0), ('alignment', 3), ('layout_error', ''),
                         ('layout_error', 'forged unavailable')]:
            value = copy.deepcopy(self.document); value['bodies'][0]['locals'][0][key] = bad
            with self.subTest(key=key, bad=bad): self.refuses(value)
        value = copy.deepcopy(self.document); value['bodies'][0]['locals'].append(value['bodies'][0]['locals'][0])
        self.refuses(value)
        for key, bad in [('phase', 'Runtime(Optimized)'), ('local_count', True),
                         ('argument_count', 6), ('block_count', 0), ('locals', {}), ('sites', {})]:
            value = copy.deepcopy(self.document); value['bodies'][0][key] = bad
            with self.subTest(key=key): self.refuses(value)

    def test_invalid_ownership_sites_and_unknown_skipped_reasons_refuse(self) -> None:
        for key, bad in [('kind', []), ('local', True), ('local', 5), ('other', 6),
                         ('block', 1), ('statement', False), ('projected', 0), ('cleanup', None),
                         ('unwind', 'forged')]:
            value = copy.deepcopy(self.document); value['bodies'][0]['sites'][0][key] = bad
            with self.subTest(key=key): self.refuses(value)
        for update in ({'reason': 'unknown'}, {'function': self.declaration(self.app, 'main')}):
            value = copy.deepcopy(self.document); value['skipped'][0].update(update)
            self.refuses(value)
        value = copy.deepcopy(self.document); value['skipped'].append(copy.deepcopy(value['skipped'][0]))
        self.refuses(value)

    def test_original_source_changes_foreign_paths_disabled_language_and_symlinks_refuse(self) -> None:
        self.read()
        original = self.model.read_bytes(); self.model.write_bytes(original + b'// changed\n')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            lifetimes.read(self.path, self.policy, self.root, self.sources)
        self.model.write_bytes(original)
        for sources in ({}, {str(self.path): hashlib.sha256(self.path.read_bytes()).hexdigest()}):
            with self.assertRaises(ValueError):
                lifetimes.read(self.path, self.policy, self.root, sources)
        disabled = Policy.parse({'schema_version': 3, 'languages': ['java']})
        with self.assertRaisesRegex(ValueError, 'requires Rust'):
            lifetimes.read(self.path, disabled, self.root, self.sources)
        link = self.base / 'link.json'; link.symlink_to(self.path)
        with self.assertRaises((ValueError, OSError)):
            lifetimes.read(link, self.policy, self.root, self.sources)


if __name__ == '__main__':
    unittest.main()
