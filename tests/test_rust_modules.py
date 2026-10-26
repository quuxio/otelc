"""Literal module statements cannot invent original source or caller routes."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from otelc import rust_modules


class RustModuleInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'app'
        self.root.mkdir()
        self.entry = self.root / 'main.rs'
        self.entry.write_text('mod private; mod inline {} fn main() {}\n')
        self.module = self.root / 'private.rs'
        self.module.write_text('struct Private(i32); fn work() {}\n')
        self.sources = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (self.entry, self.module)}
        self.path = self.base / 'modules.json'
        self.value = {'schema_version': 1, 'kind': 'otelc.rust.module-inventory',
                      'mode': 'module-inspect', 'crate_name': 'app',
                      'byte_coordinates': 'original-utf8', 'sources': self.sources,
                      'source_complete': False, 'modules': [
                          self.row('', None, 'crate', self.entry, self.entry),
                          self.row('inline', '', 'inline', self.entry, self.entry),
                          self.row('private', '', 'external', self.entry, self.module)]}

    @staticmethod
    def location(path: Path) -> dict:
        return {'file': str(path), 'start': 0, 'end': path.stat().st_size, 'expanded': False}

    def row(self, name: str, parent: str | None, kind: str, declaration: Path, body: Path) -> dict:
        return {'compiler_name': name, 'parent': parent, 'kind': kind,
                'declaration': self.location(declaration), 'body': self.location(body),
                'eligible': True, 'reason': None}

    def read(self, value=None):
        self.path.write_text(json.dumps(self.value if value is None else value))
        return rust_modules.read(self.path, self.root, self.entry, self.sources, crate_name='app')

    def test_original_crate_inline_and_external_modules_keep_distinct_physical_locations(self) -> None:
        result = self.read()
        self.assertEqual(result, self.value)
        self.assertFalse(result['source_complete'])
        self.assertNotEqual(result['modules'][2]['declaration']['file'], result['modules'][2]['body']['file'])

    def test_expanded_and_block_local_modules_remain_unavailable_including_descendants(self) -> None:
        for reason in ('expanded', 'block_module'):
            value = copy.deepcopy(self.value)
            parent = value['modules'][1]
            parent.update(eligible=False, reason=reason)
            if reason == 'expanded':
                parent['body']['expanded'] = True
            else:
                parent['parent'] = 'function'
                parent['compiler_name'] = 'function::inline'
            child = copy.deepcopy(parent)
            child['compiler_name'] += '::child'
            child['parent'] = parent['compiler_name']
            child['body']['expanded'] = False
            value['modules'].append(child)
            value['modules'].sort(key=lambda row: row['compiler_name'])
            with self.subTest(reason=reason):
                self.assertEqual(self.read(value), value)
                child.update(eligible=True, reason=None)
                with self.assertRaises(ValueError):
                    self.read(value)

    def test_closed_header_types_version_source_and_completeness_are_required(self) -> None:
        for field, replacement in [('schema_version', True), ('schema_version', 2), ('kind', 'foreign'),
                                   ('mode', 'inspect'), ('crate_name', 'foreign'),
                                   ('byte_coordinates', 'characters'),
                                   ('sources', {}), ('source_complete', True),
                                   ('modules', []), ('modules', {}), ('extra', 1)]:
            value = copy.deepcopy(self.value)
            value[field] = replacement
            with self.subTest(field=field, replacement=replacement), self.assertRaises(ValueError):
                self.read(value)
        for value in ([], {}, {'schema_version': 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.read(value)

    def test_records_refuse_extra_fields_wrong_types_ranges_and_unbound_source_names(self) -> None:
        mutations = [('extra', 1), ('compiler_name', ''), ('compiler_name', 'a' * 4097),
                     ('compiler_name', 'bad\0name'), ('kind', 'foreign'), ('eligible', 1),
                     ('reason', 'foreign'), ('reason', 'expanded')]
        for field, replacement in mutations:
            value = copy.deepcopy(self.value)
            value['modules'][1][field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.read(value)
        for field, replacement in [('file', 'foreign'), ('start', True), ('end', False),
                                   ('start', -1), ('end', 1000), ('expanded', 0), ('extra', 1)]:
            value = copy.deepcopy(self.value)
            value['modules'][1]['body'][field] = replacement
            with self.subTest(location=field), self.assertRaises(ValueError):
                self.read(value)
        for target in ('body', 'declaration'):
            value = copy.deepcopy(self.value)
            value['modules'][1][target] = []
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.read(value)

    def test_root_kind_parent_order_duplicates_and_inherited_reasons_cannot_be_forged(self) -> None:
        for kind in ('root-parent', 'root-file', 'root-range', 'root-ineligible', 'wrong-parent',
                     'missing-parent', 'wrong-kind', 'duplicate', 'order', 'missing-root',
                     'block-reason', 'expanded-reason', 'expanded-eligible', 'record-type'):
            value = copy.deepcopy(self.value)
            root, inline, external = value['modules']
            if kind == 'root-parent': root['parent'] = 'foreign'
            elif kind == 'root-file':
                root['body'] = root['declaration'] = self.location(self.module)
            elif kind == 'root-range': root['body']['start'] = 1
            elif kind == 'root-ineligible': root.update(eligible=False, reason='block_module')
            elif kind == 'wrong-parent': inline['parent'] = 'foreign'
            elif kind == 'missing-parent':
                inline.update(compiler_name='absent::inline', parent='absent')
                value['modules'].sort(key=lambda row: row['compiler_name'])
            elif kind == 'wrong-kind': external['kind'] = 'inline'
            elif kind == 'duplicate': value['modules'].append(copy.deepcopy(inline))
            elif kind == 'order': value['modules'].reverse()
            elif kind == 'missing-root': del value['modules'][0]
            elif kind == 'block-reason': inline.update(eligible=False, reason='block_module')
            elif kind == 'expanded-reason': inline.update(eligible=False, reason='expanded')
            elif kind == 'expanded-eligible': inline['body']['expanded'] = True
            else: value['modules'][1] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.read(value)

    def test_changed_sources_foreign_paths_and_invalid_input_catalogues_refuse(self) -> None:
        self.path.write_text(json.dumps(self.value))
        for sources in ({}, [], {True: 'a'}, {str(self.entry): True}):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                rust_modules.read(self.path, self.root, self.entry, sources, crate_name='app')
        for path in (Path('relative'), self.base):
            sources = self.sources | {str(path): 'a'}
            with self.subTest(path=path), self.assertRaises(ValueError):
                rust_modules.read(self.path, self.root, self.entry, sources, crate_name='app')
        self.module.write_text(self.module.read_text() + '// changed\n')
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            rust_modules.read(self.path, self.root, self.entry, self.sources, crate_name='app')

    def test_exact_relative_root_identity_and_caller_held_crate_name_are_separate(self) -> None:
        result = self.read()
        self.assertEqual(result['modules'][0]['compiler_name'], '')
        self.assertEqual(result['modules'][1]['parent'], '')
        for name in ('foreign', '', None, True):
            with self.subTest(name=name), self.assertRaises(ValueError):
                rust_modules.read(self.path, self.root, self.entry, self.sources, crate_name=name)
        value = copy.deepcopy(self.value)
        value['modules'][0]['compiler_name'] = 'app'
        with self.assertRaises(ValueError):
            self.read(value)

    def test_module_declaration_and_inline_body_must_fit_the_original_parent_body(self) -> None:
        other = self.root / 'other.rs'
        other.write_text('// another original file\n')
        self.sources[str(other)] = hashlib.sha256(other.read_bytes()).hexdigest()
        for kind in ('foreign-declaration', 'parent-interval', 'inline-interval'):
            value = copy.deepcopy(self.value)
            root, inline, external = value['modules']
            if kind == 'foreign-declaration':
                external['declaration'] = self.location(other)
            elif kind == 'parent-interval':
                root['body']['end'] = root['declaration']['end'] = 4
            else:
                inline['declaration']['end'] = 3
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.read(value)


if __name__ == '__main__':
    unittest.main()
