"""Closed emitted-site and C-only recipe checks, independent of SDK launch."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from otelc import native_resource_build as resource


class ResourceBuildRecordTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.site = {'caller': 'work', 'callee': 'malloc', 'type_usr': 'c:@S@Item',
            'location': {'debug_file': 'src/main.c', 'debug_line': 3, 'debug_column': 7}}
        self.document = {'source': str(self.base / 'src/main.c'),
            'compile_context': {'directory': str(self.base)}, 'allocations': [self.site], 'releases': []}
        self.record = {'schema_version': 1, 'kind': 'otelc.native.c-resource-resolution',
            'source': self.document['source'], 'sites': [{'caller': 'work', 'callee': 'malloc',
            'type_usr': 'c:@S@Item', 'file': self.document['source'], 'line': 3, 'column': 7,
            'emitted_calls': 2, 'emitted_callees': ['malloc', 'calloc']}]}

    def check(self, value):
        (self.base / 'resolution.json').write_text(json.dumps(value))
        resource.resolution(self.document, self.base)

    def test_inlining_family_rewrite_and_zero_match_keep_the_original_site(self):
        self.check(self.record)
        empty = copy.deepcopy(self.record)
        empty['sites'][0].update(emitted_calls=0, emitted_callees=[])
        self.check(empty)
        self.document['allocations'] = []
        self.document['releases'] = [{key: item for key, item in {**self.site, 'callee': 'free'}.items()
                                      if key != 'type_usr'}]
        released = copy.deepcopy(self.record)
        released['sites'][0].update(callee='free', type_usr='', emitted_calls=1, emitted_callees=['free'])
        self.check(released)

    def test_missing_extra_retyped_and_foreign_emitted_boundaries_refuse(self):
        variants = []
        for key, value in (('schema_version', True), ('kind', 'other'), ('source', '/other.c'), ('sites', [])):
            variants.append({**self.record, key: value})
        for key, value in (('caller', 'other'), ('file', '/foreign.c'), ('column', True),
                           ('emitted_calls', True), ('emitted_calls', -1), ('emitted_calls', 3),
                           ('emitted_callees', ['malloc', 'free']), ('extra', 'unknown')):
            changed = copy.deepcopy(self.record); changed['sites'][0][key] = value
            variants.append(changed)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.check(value)

    def test_c_application_denominator_excludes_neutral_driver_and_refuses_cpp(self):
        unit = {'id': 'application', 'role': 'application', 'language': 'c'}
        driver = {'id': 'driver', 'role': 'driver', 'language': 'cpp'}
        recipe = {'units': [unit, driver]}
        self.assertEqual(resource.units(recipe), [unit])
        self.assertEqual(set(resource.environment(unit, self.base)),
                         {'OTELC_RESOURCE_INVENTORY', 'OTELC_RESOURCE_RESOLUTION'})
        for selected in ([], [driver], [{**unit, 'language': 'cpp'}]):
            with self.assertRaises(ValueError):
                resource.units({'units': selected})

    def test_only_actual_standard_c_symbol_definitions_are_interposition(self):
        path = self.base / 'symbols.stdout'
        for text in ('', '_main\n', '_ZN4work4freeEv\n', 'allocation_malloc\n'):
            path.write_text(text)
            resource.verify_symbols(path)
        for name in resource.INTERPOSED:
            path.write_text('_main\n' + name + '\n')
            with self.subTest(symbol=name), self.assertRaisesRegex(ValueError, 'interposed'):
                resource.verify_symbols(path)
        path.write_bytes(b'\0')
        with self.assertRaisesRegex(ValueError, 'symbol record'):
            resource.verify_symbols(path)


if __name__ == '__main__':
    unittest.main()
