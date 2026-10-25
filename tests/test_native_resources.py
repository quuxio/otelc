"""Real Clang C allocation bindings, source preservation and explicit refusals."""
from __future__ import annotations

import copy
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from otelc import native_gate, native_lifetimes as tools, native_resources as resources
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'typedef struct WorkItem {int seed;} Alias;\n'


class CResourceSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.keep = os.environ.get('OTELC_CPP_ANALYSIS') == '1'
        cls.temporary = tempfile.TemporaryDirectory(prefix='c-resources-', dir=ROOT / 'build', delete=not cls.keep)
        cls.base = Path(cls.temporary.name).resolve()
        if selected := os.environ.get('OTELC_C_RESOURCE_TOOL'):
            directory = Path(selected)
            cls.tool = tools.Tool(directory, (directory / tools.MANIFEST).read_text(), tools.RESOURCE_PROFILE)
        else:
            cls.tool = tools.build_tool(cls.base / 'tool', profile=tools.RESOURCE_PROFILE, coverage=True)
        cls.tool.verify()

    @classmethod
    def tearDownClass(cls):
        receipt = cls.tool.verify()
        binaries = Path(receipt['toolchain']['directory'])
        profiles = list(cls.base.glob('resource-*.profraw'))
        assert profiles, 'C resource helper produced no native coverage profiles'
        merged = ROOT / 'build/native-resources.profdata'
        subprocess.run([str(binaries / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles),
                        '-o', str(merged)], check=True, capture_output=True, timeout=60)
        command = [str(binaries / 'llvm-cov'), 'export', receipt['product']['path'],
                   '-instr-profile=' + str(merged), str(tools.RESOURCE_SOURCE)]
        data = json.loads(subprocess.check_output(command, timeout=60))
        files = [row for block in data['data'] for row in block['files']
                 if Path(row['filename']).resolve() == tools.RESOURCE_SOURCE]
        assert len(files) == 1
        summary = files[0]['summary']['lines']
        lcov = ROOT / 'build/native-resources.lcov'
        lcov.write_bytes(subprocess.check_output([*command, '-format=lcov'], timeout=60))
        (ROOT / 'build/native-resources-coverage.json').write_text(json.dumps({
            'source': native_gate.identity(tools.RESOURCE_SOURCE), 'tool': receipt,
            'line_coverage': summary, 'lcov': native_gate.identity(lcov)}, indent=2) + '\n')
        print('C resource Clang coverage:', summary)
        assert summary['percent'] >= 80, summary
        if not cls.keep:
            cls.temporary.cleanup()

    def setUp(self):
        self.folder = type(self).base / self._testMethodName
        self.folder.mkdir()
        self.root = self.folder / 'application'; self.root.mkdir()
        self.source = self.root / 'main.c'
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['c'],
            'sources': {'include': ['**']}, 'lifetimes': {'enabled': True, 'include': ['WorkItem']}})

    def inspect(self, source, *, policy=None, context=None, output='inventory'):
        self.source.write_text(source)
        before = native_gate.identity(self.source)
        with patch.dict(os.environ, LLVM_PROFILE_FILE=str(type(self).base / 'resource-%m-%p.profraw')):
            value = resources.inspect(policy or self.policy, self.root, self.source,
                self.folder / output, tool=self.tool, context=context)
        self.assertEqual(native_gate.identity(self.source), before)
        return value

    def test_real_calls_resolve_typedefs_casts_assignments_and_return_types(self):
        source = '#include <stdlib.h>\n' + MODEL + '''
Alias *make(int n){return calloc(n,sizeof(Alias));}
Alias *workload(void){Alias *p=malloc(sizeof *p);Alias *q;
 q=(Alias*)calloc(1,sizeof *q);free((void*)p);free(q);return make(1);}
'''
        value = self.inspect(source)
        self.assertEqual([row['name'] for row in value['types']], ['WorkItem'])
        self.assertEqual(len(value['allocations']), 3)
        self.assertEqual(len(value['releases']), 2)
        self.assertEqual({row['caller'] for row in value['allocations']}, {'make', 'workload'})
        self.assertTrue(all(row['standard'] for row in value['allocations'] + value['releases']))
        self.assertFalse(value['source_complete'])

    def test_anonymous_record_with_an_existing_typedef_keeps_its_public_name(self):
        value = self.inspect('#include <stdlib.h>\ntypedef struct {int seed;} WorkItem;\n'
            'void work(void){WorkItem*p=malloc(sizeof *p);free(p);}\n')
        self.assertEqual([row['name'] for row in value['types']], ['WorkItem'])
        self.assertEqual(len(value['allocations']), 1)

    def test_unrelated_call_arguments_and_unevaluated_expressions_are_not_allocations(self):
        value = self.inspect('#include <stdlib.h>\n' + MODEL + '''
Alias*convert(void*p){return p;}
Alias*work(void){Alias*p=convert(malloc(32));(void)sizeof(malloc(32));return p;}
''')
        self.assertEqual(value['allocations'], [])

    def test_scope_reallocation_and_interposed_functions_refuse_before_publication(self):
        sources = {
            'scope': MODEL + 'void work(void){Alias value={1};}\n',
            'realloc': '#include <stdlib.h>\n' + MODEL + 'void work(Alias*p){p=realloc(p,32);}\n',
            'malloc': '#include <stdlib.h>\n' + MODEL + 'void*malloc(size_t n){return 0;}'
                      'Alias*work(void){return malloc(32);}\n',
            'free': '#include <stdlib.h>\nvoid free(void*p){}\nvoid work(void*p){free(p);}\n',
            'incomplete': '#include <stdlib.h>\nstruct WorkItem;'
                          'struct WorkItem*work(void){return malloc(32);}\n',
        }
        for name, source in sources.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.inspect(source, output=name)
            self.assertFalse((self.folder / name / resources.MANIFEST).exists())

    def test_no_profile_helpers_and_exclusions_do_not_create_observer_lifetimes(self):
        source = '#include <stdlib.h>\n' + MODEL + '''
__attribute__((no_profile_instrument_function)) void observer(void){Alias*p=malloc(32);free(p);}
Alias*work(void){return malloc(32);}
'''
        value = self.inspect(source)
        self.assertEqual([row['caller'] for row in value['allocations']], ['work'])
        data = copy.deepcopy(self.policy.data); data['lifetimes']['exclude'] = ['WorkItem']
        excluded = self.inspect(source, policy=Policy.parse(data), output='excluded')
        self.assertEqual(excluded['allocations'], [])

    def test_actual_directory_flags_and_system_header_are_retained(self):
        include = self.root / 'include'; include.mkdir()
        (include / 'item.h').write_text(MODEL)
        unit = self.root / 'unit'; unit.mkdir()
        self.source = unit / 'main.c'
        context = {'directory': str(unit), 'flags': ['-std=c17', '-O1', '-isystem', '../include', '-D', 'SIZE=32'],
                   'overlay': None, 'assets': []}
        value = self.inspect('#include <stdlib.h>\n#include "item.h"\nAlias*work(void){return malloc(SIZE);}\n',
                             context=context)
        self.assertEqual(value['compile_context'], context)
        self.assertIn(str(include / 'item.h'), {row['path'] for row in value['inputs']})

    def test_private_vfs_helpers_do_not_replace_source_or_add_observations(self):
        source = '#include <stdlib.h>\n' + MODEL + 'void work(void){Alias*p=malloc(32);free(p);}\n'
        self.source.write_text(source)
        private = self.folder / 'private'; private.mkdir()
        copy_source = private / 'main.c'
        copy_source.write_text(source + '__attribute__((no_profile_instrument_function)) '
                               'void observer(void){Alias*p=malloc(32);free(p);}\n')
        overlay = private / 'overlay.json'
        overlay.write_text(json.dumps({'version': 0, 'use-external-names': False, 'roots': [
            {'type': 'file', 'name': str(self.source), 'external-contents': str(copy_source)}]}) + '\n')
        context = {'directory': str(self.root), 'flags': ['-std=c11', '-O1'],
                   'overlay': str(overlay), 'assets': [str(copy_source), str(overlay)]}
        value = self.inspect(source, context=context)
        self.assertEqual([row['caller'] for row in value['allocations']], ['work'])
        self.assertEqual([row['caller'] for row in value['releases']], ['work'])
        self.assertEqual(len(value['context_inputs']), 2)

    def test_changed_header_refuses_after_the_actual_semantic_query(self):
        header = self.root / 'item.h'; header.write_text(MODEL)
        run = tools._run
        def changed(command, root, output, label):
            run(command, root, output, label)
            if label == 'semantics':
                header.write_text(MODEL + '/* changed after compiler read */\n')
        with patch.object(tools, '_run', side_effect=changed), self.assertRaisesRegex(ValueError, 'inputs changed'):
            self.inspect('#include <stdlib.h>\n#include "item.h"\nAlias*work(void){return malloc(32);}\n')

    def test_semantic_claims_cannot_substitute_unknown_or_duplicate_types(self):
        value = self.inspect('#include <stdlib.h>\n' + MODEL + 'Alias*work(void){return malloc(32);}\n')
        raw = json.loads(gzip.decompress(Path(value['semantic']['path']).read_bytes()))
        for kind in ('duplicate', 'missing', 'schema'):
            altered = copy.deepcopy(raw)
            if kind == 'duplicate': altered['types'].append(copy.deepcopy(altered['types'][0]))
            elif kind == 'missing': altered['types'] = []
            else: altered['schema_version'] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                resources.catalogue(altered, self.policy, self.root)

    def test_wrong_profile_source_outputs_and_contexts_refuse_without_writes(self):
        self.source.write_text('int main(void){return 0;}\n')
        wrong = tools.Tool(self.tool.directory, self.tool.encoded)
        with self.assertRaisesRegex(ValueError, 'fixed C resource helper'):
            resources.inspect(self.policy, self.root, self.source, self.folder / 'wrong', tool=wrong)
        for output in (self.root / 'output', self.root.with_name(self.root.name.upper()) / 'output'):
            if not output.parent.exists(): continue
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, 'outside'):
                resources.inspect(self.policy, self.root, self.source, output, tool=self.tool)
            self.assertFalse(output.exists())
        with self.assertRaises(ValueError):
            tools.build_tool(self.folder / 'bad-tool', profile='application-selected-helper')
        self.assertFalse((self.folder / 'bad-tool').exists())

    def test_helper_refuses_cpp_bad_arguments_and_invalid_working_directories(self):
        compiler = Path(self.tool.verify()['toolchain']['directory'])
        cpp = self.folder / 'main.cpp'; cpp.write_text('int main(){return 0;}\n')
        helper = str(self.tool.directory / 'lifetimes')
        cases = [[], [str(self.root), str(cpp), '--compile-directory'],
            [str(self.root), str(cpp), '--compile-directory', str(self.folder / 'missing'), str(compiler / 'clang++')],
            [str(self.root), str(cpp), str(compiler / 'clang++'), '-std=c++17']]
        for words in cases:
            result = subprocess.run([helper, *words], capture_output=True, timeout=20,
                env=dict(os.environ, LLVM_PROFILE_FILE=str(type(self).base / 'resource-%m-%p.profraw')))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b'c-resource-semantics', result.stdout)


if __name__ == '__main__':
    unittest.main()
