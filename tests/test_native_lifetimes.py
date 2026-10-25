"""Real Clang semantics for source-free native lifetime probe selection."""
from __future__ import annotations

import copy
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelc import native_lifetimes as lifetimes
from otelc.config import Policy

MODEL = '''#pragma once
namespace demo {
struct BaseItem { BaseItem() {} virtual ~BaseItem() {} };
struct WorkItem : virtual BaseItem {
 int value;
 explicit WorkItem(int input) : BaseItem(), value(input) { if(input < 0) throw input; }
 WorkItem() : WorkItem(1) {}
 WorkItem(const WorkItem &other) : BaseItem(), value(other.value) {}
 WorkItem(WorkItem &&other) : BaseItem(), value(other.value) {}
 ~WorkItem() override {}
 void close() {}
};
using Alias = WorkItem;
template<class T> struct BoxItem { T value; explicit BoxItem(T input):value(input){} };
struct TrivialItem { int value; };
}
'''
APP = '''#include "model.hpp"
#define BUILD_ITEM(value) demo::WorkItem(value)
demo::WorkItem factory() { return BUILD_ITEM(5); }
int main() {
 demo::WorkItem first(1);
 demo::Alias copy(first);
 demo::WorkItem moved(static_cast<demo::WorkItem&&>(copy));
 demo::WorkItem array[2];
 auto *heap = new demo::Alias(7);
 demo::BoxItem<int> box(4);
 demo::TrivialItem plain{7};
 auto returned = factory();
 try { demo::WorkItem failed(-1); } catch(int) {}
 first.close();
 delete heap;
 return first.value + box.value + plain.value + returned.value + moved.value + array[0].value;
}
'''


class NativeLifetimeInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (lifetimes.ROOT / 'build').mkdir(exist_ok=True)
        cls.keep = os.environ.get('OTELC_CPP_ANALYSIS') == '1'
        cls.tool_temporary = tempfile.TemporaryDirectory(prefix='native-lifetimes-',
            dir=lifetimes.ROOT / 'build', delete=not cls.keep)
        cls.tool = lifetimes.build_tool(Path(cls.tool_temporary.name) / 'tool', coverage=True)

    @classmethod
    def tearDownClass(cls) -> None:
        record = cls.tool.verify()
        base = cls.tool.directory.parent
        binaries = Path(record['toolchain']['directory'])
        output = lifetimes.ROOT / 'build'
        merged = output / 'native-lifetimes.profdata'
        subprocess.run([str(binaries / 'llvm-profdata'), 'merge', '-sparse',
            *map(str, base.glob('*.profraw')), '-o', str(merged)],
            check=True, capture_output=True, timeout=60)
        command = [str(binaries / 'llvm-cov'), 'export', record['product']['path'],
            '-instr-profile=' + str(merged), str(lifetimes.SOURCE)]
        data = json.loads(subprocess.check_output(command, timeout=60))
        files = [row for block in data['data'] for row in block['files']
                 if Path(row['filename']).resolve() == lifetimes.SOURCE]
        assert len(files) == 1
        summary = files[0]['summary']['lines']
        lcov = output / 'native-lifetimes.lcov'
        lcov.write_bytes(subprocess.check_output([*command, '-format=lcov'], timeout=60))
        database = output / 'native-lifetimes-compile-commands.json'
        shutil.copyfile(cls.tool.directory / 'compile_commands.json', database)
        (output / 'native-lifetimes-coverage.json').write_text(json.dumps({
            'source': lifetimes.llvm_backend.identity(lifetimes.SOURCE),
            'inputs': record['inputs'], 'tool': record,
            'lcov': lifetimes.llvm_backend.identity(lcov),
            'compilation_database': lifetimes.llvm_backend.identity(database),
            'line_coverage': summary}, indent=2) + '\n')
        print('Clang lifetime coverage:', summary)
        assert summary['percent'] >= 80
        if not cls.keep:
            cls.tool_temporary.cleanup()

    def setUp(self) -> None:
        profiles = patch.dict(os.environ, LLVM_PROFILE_FILE=str(self.tool.directory.parent / '%m-%p.profraw'))
        profiles.start()
        self.addCleanup(profiles.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix='otelc-lifetime-inventory-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'application'; self.root.mkdir()
        self.header, self.source = self.root / 'model.hpp', self.root / 'main.cpp'
        self.header.write_text(MODEL)
        self.source.write_text(APP)
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['cpp'],
            'sources': {'include': ['**']}, 'lifetimes': {'enabled': True, 'include': ['*Item*']}})

    def inspect(self, name: str, policy: Policy | None = None, **options) -> dict:
        return lifetimes.inspect(policy or self.policy, self.root, [Path('main.cpp')], self.base / name, tool=self.tool, **options)

    def test_actual_per_unit_directory_defines_standard_and_optimisation_are_used(self) -> None:
        directory = self.root / 'unit'; directory.mkdir()
        includes = self.root / 'include'; includes.mkdir()
        header = includes / 'context.hpp'
        header.write_text('namespace build_context {template<int I> struct ContextItem {'
                          'ContextItem(){} ~ContextItem(){} int value=I;};}\n')
        source = directory / 'context.cpp'
        source.write_text('#include "context.hpp"\n'
            '#if !defined(SCALE) || !defined(__OPTIMIZE__)\n#error missing actual compile flags\n#endif\n'
            'template<class T> concept Addable = requires(T v){v+1;};\n'
            'int run(){static_assert(Addable<int>);build_context::ContextItem<SCALE> item;return item.value;}\n')
        original = source.read_bytes(), header.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Clang lifetime query failed'):
            lifetimes.inspect(self.policy, self.root, [source], self.base / 'wrong-default', tool=self.tool)
        context = {'directory': str(directory), 'flags': ['-std=c++20', '-O1', '-D', 'SCALE=5', '-I', '../include'],
                   'overlay': None, 'assets': []}
        value = lifetimes.inspect(self.policy, self.root, [source], self.base / 'actual-context',
                                  tool=self.tool, context=context)
        self.assertEqual((value['schema_version'], value['profile']), (2, lifetimes.CONTEXT_PROFILE))
        self.assertEqual(value['compile_context'], context)
        self.assertEqual(value['root'], str(self.root))
        self.assertEqual({row['name'] for row in value['translation_units'][0]['types']},
                         {'build_context::ContextItem<5>'})
        self.assertEqual(len(value['translation_units'][0]['constructions']), 1)
        semantics = value['commands'][1]
        self.assertEqual(semantics[3:5], ['--compile-directory', str(directory)])
        self.assertIn('-O1', semantics)
        compiler = value['tool']['toolchain']['directory'] + '/clang++'
        result = subprocess.run([compiler, *context['flags'], '-g', '-fsyntax-only', str(source)],
                                cwd=directory, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        context['flags'].append('-O0')
        self.assertNotIn('-O0', value['compile_context']['flags'])
        self.assertEqual((source.read_bytes(), header.read_bytes()), original)

    def test_private_overlay_inventory_keeps_original_and_private_inputs_distinct(self) -> None:
        self.source.write_text('struct PlainItem{PlainItem(){}~PlainItem(){}};int main(){PlainItem item;}\n')
        original = self.source.read_bytes()
        private = self.base / 'overlay.cpp'
        private.write_text('struct OverlayItem{OverlayItem(){}~OverlayItem(){}};int main(){OverlayItem item;}\n')
        overlay = self.base / 'overlay.json'
        overlay.write_text(json.dumps({'version': 0, 'use-external-names': False, 'roots': [
            {'type': 'file', 'name': str(self.source), 'external-contents': str(private)}]}) + '\n')
        context = {'directory': str(self.root), 'flags': ['-std=c++17', '-O2'],
                   'overlay': str(overlay), 'assets': [str(private), str(overlay)]}
        value = self.inspect('private-context', context=context)
        self.assertEqual({row['name'] for row in value['translation_units'][0]['types']}, {'OverlayItem'})
        self.assertEqual(value['translation_units'][0]['source'], str(self.source))
        self.assertEqual({row['path'] for row in value['context_inputs']}, {str(private), str(overlay)})
        self.assertEqual(self.source.read_bytes(), original)
        missing = copy.deepcopy(context); missing['assets'] = [str(overlay)]
        with self.assertRaisesRegex(ValueError, 'VFS input'):
            self.inspect('unbound-private-context', context=missing)
        self.assertFalse((self.base / 'unbound-private-context').exists())

    def test_context_refuses_extra_fields_tooling_flags_escaped_cwd_and_multiple_units(self) -> None:
        context = {'directory': str(self.root), 'flags': ['-std=c++17'], 'overlay': None, 'assets': []}
        for kind in ('extra', 'plugin', 'directory', 'multiple'):
            value = copy.deepcopy(context)
            sources = [self.source]
            if kind == 'extra': value['unknown'] = True
            elif kind == 'plugin': value['flags'].append('-fpass-plugin=/operator-input')
            elif kind == 'directory': value['directory'] = str(self.base)
            else:
                other = self.root / 'other.cpp'; other.write_bytes(self.source.read_bytes())
                sources.append(other)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                lifetimes.inspect(self.policy, self.root, sources, self.base / ('invalid-' + kind),
                                  tool=self.tool, context=value)
            self.assertFalse((self.base / ('invalid-' + kind)).exists())

    def test_real_ast_distinguishes_delegation_bases_copy_move_arrays_aliases_and_aggregate(self) -> None:
        original = self.header.read_bytes(), self.source.read_bytes()
        value = self.inspect('inventory')
        unit = value['translation_units'][0]
        self.assertEqual(unit['unresolved'], [])
        types = {row['name']: row for row in unit['types']}
        self.assertIn('demo::WorkItem', types)
        self.assertIn('demo::BoxItem<int>', types)
        self.assertIn('demo::TrivialItem', types)
        methods = types['demo::WorkItem']['methods']
        self.assertEqual(sum(row['delegating'] for row in methods), 1)
        self.assertTrue(any(row['symbol'].startswith('_Z') for row in methods if row['symbol']))
        sites = unit['constructions']
        self.assertTrue(any(row['construction_kind'] == 'delegating' for row in sites))
        self.assertTrue(any(row['construction_kind'] == 'virtual base' for row in sites))
        self.assertTrue(any(row['array_type'] is not None for row in sites))
        self.assertTrue(any(row['kind'] == 'InitListExpr' and row['type'] == 'demo::TrivialItem' for row in sites))
        variables = {row['variable']: row for row in unit['variables']}
        self.assertEqual(variables['returned']['type'], 'demo::WorkItem')
        self.assertEqual(variables['plain']['type'], 'demo::TrivialItem')
        self.assertNotIn('heap', variables)
        signatures = {row['constructor_signature'] for row in sites}
        self.assertTrue(any(signature and '&&' in signature for signature in signatures))
        self.assertTrue(any(signature and '&' in signature and '&&' not in signature for signature in signatures))
        for row in sites:
            location = row['location']
            raw = Path(location['file']).read_bytes()
            self.assertLess(location['offset'], len(raw))
            self.assertEqual(location['line'], raw[:location['offset']].count(b'\n') + 1)
        compressed = Path(unit['semantic']['path'])
        self.assertTrue(compressed.exists())
        self.assertEqual(len(gzip.decompress(compressed.read_bytes())), unit['semantic_bytes'])
        self.assertFalse(compressed.with_suffix('').exists())
        self.assertEqual((self.header.read_bytes(), self.source.read_bytes()), original)
        self.assertEqual({Path(row['path']).name for row in value['inputs']}, {'main.cpp', 'model.hpp'})
        self.assertFalse(value['source_complete'])

    def test_alias_ids_and_out_of_line_delegation_do_not_guess_another_namespaces_type(self) -> None:
        self.header.write_text(MODEL + '\nnamespace other { struct WorkItem { WorkItem(int){} }; }\n'
            'namespace demo { using SecondAlias=Alias; struct OtherItem { OtherItem(int); OtherItem(); }; }\n')
        self.source.write_text('#include "model.hpp"\n'
            'demo::OtherItem::OtherItem(int){}\ndemo::OtherItem::OtherItem():OtherItem(1){}\n'
            'int main(){demo::SecondAlias object(7); other::WorkItem other(8); demo::OtherItem delegated;}\n')
        unit = self.inspect('alias-ids')['translation_units'][0]
        self.assertEqual(unit['unresolved'], [])
        variables = {row['variable']: row['type'] for row in unit['variables']}
        self.assertEqual(variables['object'], 'demo::WorkItem')
        self.assertEqual(variables['other'], 'other::WorkItem')
        methods = next(row['methods'] for row in unit['types'] if row['name'] == 'demo::OtherItem')
        self.assertEqual(sum(row['delegating'] for row in methods), 1)

    def test_common_type_and_source_exclusions_apply_to_the_semantic_inventory(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['lifetimes']['exclude'] = ['*WorkItem']
        value = self.inspect('excluded-type', Policy.parse(data))
        self.assertTrue(all(row['type'] != 'demo::WorkItem' for row in value['translation_units'][0]['constructions']))
        data['sources']['exclude'] = ['model.hpp']
        value = self.inspect('excluded-source', Policy.parse(data))
        self.assertEqual(value['translation_units'][0]['types'], [])

    def test_linux_target_has_the_same_semantic_types_without_running_a_linux_app(self) -> None:
        value = self.inspect('linux', target='x86_64-unknown-linux-gnu')
        self.assertEqual(value['target'], 'x86_64-unknown-linux-gnu')
        types = value['translation_units'][0]['types']
        self.assertTrue(any(row['name'] == 'demo::WorkItem' for row in types))
        symbols = [method['symbol'] for row in types for method in row['methods'] if method['symbol']]
        self.assertTrue(all(not name.startswith('__Z') for name in symbols))

    def test_changed_header_is_refused_before_inventory_publication(self) -> None:
        original = lifetimes.catalogue

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.header.write_text(MODEL + '\n// changed after compiler read\n')
            return result

        with patch.object(lifetimes, 'catalogue', side_effect=changed), self.assertRaisesRegex(ValueError, 'changed'):
            self.inspect('changed')
        self.assertFalse((self.base / 'changed/native-lifetimes.json').exists())

    def test_invalid_inputs_refuse_before_a_compiler_or_output_is_created(self) -> None:
        disabled = Policy.parse({'schema_version': 3, 'languages': ['cpp']})
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('unexpected compiler')):
            with self.assertRaises(ValueError):
                self.inspect('disabled', disabled)
            for sources in ([], [Path('main.cpp'), Path('main.cpp')]):
                with self.assertRaises(ValueError):
                    lifetimes.inspect(self.policy, self.root, sources, self.base / 'invalid')
        self.assertFalse((self.base / 'invalid').exists())

    def test_failed_and_oversized_query_outputs_cannot_become_inventory_data(self) -> None:
        output = self.base / 'query'; output.mkdir()
        with self.assertRaisesRegex(ValueError, 'query failed'):
            lifetimes._run([sys.executable, '-c', 'raise SystemExit(3)'], self.root, output, 'failed')
        with patch.object(lifetimes, 'MAX_AST', 16), self.assertRaisesRegex(ValueError, 'output'):
            lifetimes._run([sys.executable, '-c', 'print("x"*10000)'], self.root, output, 'large')

    def test_non_semantic_data_and_unbound_or_duplicate_types_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            lifetimes.catalogue({}, self.policy, self.root)
        document = {'schema_version': 1, 'kind': 'otelc.clang.lifetime-semantics',
            'types': [], 'variables': [], 'constructions': [{'type_usr': 'missing'}]}
        with self.assertRaisesRegex(ValueError, 'canonical type'):
            lifetimes.catalogue(document, self.policy, self.root)
        document['constructions'] = []
        document['types'] = [{'usr': 'duplicate'}, {'usr': 'duplicate'}]
        with self.assertRaisesRegex(ValueError, 'duplicated'):
            lifetimes.catalogue(document, self.policy, self.root)

    def test_changed_tool_receipt_is_refused_without_replacing_the_held_identity(self) -> None:
        receipt = self.tool.directory / lifetimes.MANIFEST
        original = receipt.read_bytes()
        try:
            receipt.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.inspect('changed-tool')
        finally:
            receipt.write_bytes(original)

    def test_parameter_static_and_thread_storage_are_distinct_from_ordinary_locals(self) -> None:
        self.source.write_text('#include "model.hpp"\n'
            'demo::WorkItem global(3);\n'
            'void consume(demo::WorkItem parameter, const demo::WorkItem &reference){\n'
            'static demo::WorkItem shared(4); thread_local demo::WorkItem per_thread(5);\n'
            'demo::WorkItem local(6); }\n')
        rows = {row['variable']: row for row in self.inspect('storage')['translation_units'][0]['variables']}
        self.assertTrue(rows['parameter']['parameter'])
        self.assertFalse(rows['parameter']['local'])
        self.assertTrue(rows['local']['local'])
        self.assertFalse(rows['local']['global_storage'])
        self.assertTrue(rows['global']['global_storage'])
        self.assertTrue(rows['shared']['static_local'])
        self.assertTrue(rows['per_thread']['thread_local'])
        self.assertNotIn('reference', rows)

    def test_canonical_template_local_and_anonymous_types_preserve_physical_line_locations(self) -> None:
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['cpp'],
            'sources': {'include': ['**']}, 'lifetimes': {'enabled': True, 'include': ['*']}})
        self.source.write_text('template<int N, bool Flag> struct Box { int value; };\nusing Alias = const Box<7, true>;\ntypedef struct { int value; } Anonymous;\n#line 900 "logical.cpp"\nint main(){ Alias object{7}; Anonymous anon{8};\n { struct Local { int value; }; Local inner{9}; }\n auto closure=[]{}; return object.value+anon.value; }\n')
        unit = self.inspect('canonical')['translation_units'][0]
        variables = {row['variable']: row for row in unit['variables']}
        self.assertEqual(variables['object']['type'], 'Box<7, true>')
        self.assertEqual(variables['anon']['type'], 'Anonymous')
        self.assertNotEqual(variables['inner']['type_usr'], variables['anon']['type_usr'])
        self.assertEqual(variables['object']['location']['line'], 5)
        self.assertEqual(variables['object']['location']['debug_line'], 900)
        self.assertEqual(variables['object']['location']['debug_file'], 'logical.cpp')
        self.assertIn('closure', variables)
