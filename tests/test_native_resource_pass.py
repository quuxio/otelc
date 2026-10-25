"""Real LLVM C allocation/free probes preserve ordinary application channels."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import unittest

from otelc import llvm_backend as backend, native_lifetimes, native_resources
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]
ELIDED_APP = '''#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <errno.h>
typedef struct WorkItem {int value;} WorkItem;
int main(void){
 WorkItem*p=malloc(sizeof *p); if(!p)return 2;p->value=13;
 WorkItem*q=calloc(1,sizeof *q);if(!q){free(p);return 3;}q->value=4;
 char*other=malloc(4);if(!other)return 4;
 int result=p->value+q->value;free((void*)p);free(q);free(other);free(NULL);
 volatile size_t huge=SIZE_MAX;WorkItem*failed=calloc(huge,sizeof *failed);
 if(failed)return 5;
 printf("%d %d\\n",result,errno==ENOMEM);return 0;
}
'''
# Ordinary volatile bindings keep allocation results observable to the compiler.
# Neither fixture has instrumentation imports, annotations or bootstrap code.
APP = (ELIDED_APP.replace('WorkItem*p=', 'WorkItem*volatile p=')
       .replace('WorkItem*q=', 'WorkItem*volatile q=')
       .replace('char*other=', 'char*volatile other=')
       .replace('WorkItem*failed=', 'WorkItem*volatile failed=')
       .replace('free(NULL);', 'void*volatile empty=NULL;free(empty);'))
OBSERVER = r'''#include "resource_abi.h"
#include "function_abi.h"
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <set>
struct Counts {std::uint64_t admitted=0,completed=0,live=0,untracked=0;};
Counts counts;
std::map<const void*,std::uint64_t> active;
std::set<std::uint64_t> pending;
extern "C" void otelc_function_register_v1(const otelc_function_descriptor_v1*,std::uint64_t n) noexcept {assert(n==0);}
extern "C" std::uint64_t otelc_function_enter_v1(const void*) noexcept{return 0;}
extern "C" void otelc_function_leave_v1(std::uint64_t,std::int32_t) noexcept{}
extern "C" void otelc_resource_allocated_v1(const void*p,const char*usr,const char*name) noexcept {
 assert(p&&usr&&name);auto token=++counts.admitted;
 assert(active.emplace(p,token).second);++counts.live;
}
extern "C" std::uint64_t otelc_resource_releasing_v1(const void*p) noexcept {
 auto found=active.find(p);if(found==active.end()){++counts.untracked;return 0;}
 auto token=found->second;active.erase(found);pending.insert(token);--counts.live;return token;
}
extern "C" void otelc_resource_released_v1(std::uint64_t token) noexcept {
 if(token){assert(pending.erase(token)==1);++counts.completed;}
}
__attribute__((destructor)) static void report(){
 const char*path=std::getenv("RESOURCE_TEST_OUTPUT");assert(path);
 auto*f=std::fopen(path,"wx");assert(f);
 std::fprintf(f,"{\"admitted\":%llu,\"completed\":%llu,\"live\":%llu,\"untracked\":%llu}\n",
  (unsigned long long)counts.admitted,(unsigned long long)counts.completed,
  (unsigned long long)counts.live,(unsigned long long)counts.untracked);std::fclose(f);
}
'''


def no_core():
    """Expected compiler refusals must not create large crash dumps."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class CResourcePassTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.keep = os.environ.get('OTELC_CPP_ANALYSIS') == '1'
        cls.temporary = tempfile.TemporaryDirectory(prefix='c-resource-pass-', dir=ROOT / 'build', delete=not cls.keep)
        cls.base = Path(cls.temporary.name).resolve()
        cls.plugin = backend.build(cls.base / 'plugin', coverage=True, function_registry=True,
                                   source_aware=True, resource_lifetimes=True)
        cls.manifest = backend.verify(cls.plugin)
        cls.binaries = Path(cls.manifest['toolchain']['directory'])
        cls.tool = native_lifetimes.build_tool(cls.base / 'tool', profile=native_lifetimes.RESOURCE_PROFILE)

    @classmethod
    def tearDownClass(cls):
        backend.verify(cls.plugin); cls.tool.verify()
        merged = ROOT / 'build/native-resource-pass.profdata'
        profiles = list(cls.base.glob('compiler-*.profraw'))
        assert profiles
        subprocess.run([str(cls.binaries / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles),
                        '-o', str(merged)], check=True, capture_output=True, timeout=60)
        source = backend.RESOURCE_INPUTS[0]
        command = [str(cls.binaries / 'llvm-cov'), 'export', cls.manifest['plugin']['path'],
                   '-instr-profile=' + str(merged), str(source)]
        data = json.loads(subprocess.check_output(command, timeout=60))
        files = [row for block in data['data'] for row in block['files'] if Path(row['filename']).resolve() == source]
        assert len(files) == 1
        summary = files[0]['summary']['lines']
        lcov = ROOT / 'build/native-resource-pass.lcov'
        lcov.write_bytes(subprocess.check_output([*command, '-format=lcov'], timeout=60))
        (ROOT / 'build/native-resource-pass-coverage.json').write_text(json.dumps({
            'source': backend.identity(source), 'plugin': cls.manifest, 'line_coverage': summary,
            'headers': [backend.identity(path) for path in (*backend.RESOURCE_INPUTS[1:], backend.HEADER)],
            'compilation_database': backend.identity(cls.plugin.directory / 'compile_commands.json'),
            'lcov': backend.identity(lcov)}, indent=2)+'\n')
        print('C resource LLVM coverage:', summary)
        assert summary['percent'] >= 80, summary
        if not cls.keep: cls.temporary.cleanup()

    def setUp(self):
        self.folder = type(self).base / self._testMethodName; self.folder.mkdir()
        self.root = self.folder / 'application'; self.root.mkdir()
        self.source = self.root / 'main.c'; self.source.write_text(APP)
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['c'],
            'sources': {'include': ['**']},
            'functions': {'include': []}, 'lifetimes': {'enabled': True, 'include': ['WorkItem']}})
        self.inventory = native_resources.inspect(self.policy, self.root, self.source,
                                                  self.folder / 'inventory', tool=self.tool)

    def compile(self, name='woven', *, value=None, raw=None, extra=()):
        filename = self.folder / (name+'.json')
        filename.write_text(raw if raw is not None else json.dumps(value or self.inventory))
        environment = backend.environment(self.policy, 'c', source_root=self.root)
        environment.update(OTELC_RESOURCE_INVENTORY=str(filename),
            OTELC_RESOURCE_RESOLUTION=str(self.folder / (name+'-resolution.json')),
            LLVM_PROFILE_FILE=str(type(self).base / 'compiler-%m-%p.profraw'))
        command = [str(self.binaries / 'clang'), '-std=c11', '-O2', '-g', '-fexceptions',
            '-fpass-plugin='+self.manifest['plugin']['path'], '-c', str(self.source),
            '-o', str(self.folder / (name+'.o')), *extra]
        result = subprocess.run(command, env=environment, capture_output=True, timeout=60, preexec_fn=no_core)
        (self.folder / (name+'.stderr')).write_bytes(result.stderr)
        return result

    def run_pair(self, *, expected_exit: int, expected_output: bytes, extra: tuple[str, ...] = ()) -> dict:
        """Compare real stock and observed processes with explicitly expected outcomes."""
        before = self.source.read_bytes()
        plain, woven = self.folder / 'plain', self.folder / 'woven'
        subprocess.run([str(self.binaries/'clang'), '-std=c11', '-O2', '-g', '-fexceptions',
                        str(self.source), '-o', str(plain), *extra], check=True, capture_output=True, timeout=60)
        result = self.compile(extra=extra)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        observer = self.folder / 'observer.cpp'; observer.write_text(OBSERVER)
        subprocess.run([str(self.binaries/'clang++'), '-std=c++17', str(self.folder/'woven.o'), str(observer),
            '-I', str(ROOT/'native/llvm'), '-o', str(woven)], check=True, capture_output=True, timeout=60)
        baseline = subprocess.run([str(plain)], capture_output=True, timeout=10)
        self.assertEqual((baseline.returncode, baseline.stdout, baseline.stderr),
                         (expected_exit, expected_output, b''))
        observed = subprocess.run([str(woven)], capture_output=True, timeout=10,
                                  env=dict(os.environ, RESOURCE_TEST_OUTPUT=str(self.folder/'observed.json')))
        self.assertEqual((observed.returncode, observed.stdout, observed.stderr),
                         (baseline.returncode, baseline.stdout, baseline.stderr))
        counts = json.loads((self.folder/'observed.json').read_text())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(json.loads((self.folder/'woven-resolution.json').read_text())['source'], str(self.source))
        return counts

    def test_unchanged_c_program_has_exact_successful_allocation_and_release_probes(self):
        counts = self.run_pair(expected_exit=0, expected_output=b'17 1\n')
        self.assertEqual(counts, {'admitted': 2, 'completed': 2, 'live': 0, 'untracked': 2})
        rows = json.loads((self.folder/'woven-resolution.json').read_text())['sites']
        self.assertEqual(sum(row['emitted_calls'] for row in rows if row['callee'] != 'free'), 3)

    def test_elided_allocations_keep_stock_outcome_without_fabricated_events(self):
        self.source.write_text(ELIDED_APP)
        self.inventory = native_resources.inspect(self.policy, self.root, self.source,
                                                  self.folder / 'elided-inventory', tool=self.tool)
        counts = self.run_pair(expected_exit=5, expected_output=b'')
        self.assertEqual(counts, {'admitted': 0, 'completed': 0, 'live': 0, 'untracked': 0})
        rows = json.loads((self.folder/'woven-resolution.json').read_text())['sites']
        self.assertTrue(rows)
        self.assertEqual(sum(row['emitted_calls'] for row in rows), 0)

    def test_unoptimised_calls_are_observed_after_validation(self):
        counts = self.run_pair(expected_exit=0, expected_output=b'17 1\n', extra=('-O0',))
        self.assertEqual(counts['admitted'], 2)
        self.assertEqual(counts['completed'], 2)

    def test_frontend_discarded_constant_branch_preserves_stock_execution(self):
        self.source.write_text('''#include <stdlib.h>
typedef struct WorkItem {int value;} WorkItem;
int main(void){if(0){WorkItem*p=malloc(sizeof *p);free(p);}return 0;}
''')
        self.inventory = native_resources.inspect(self.policy, self.root, self.source,
                                                  self.folder / 'discarded-inventory', tool=self.tool)
        counts = self.run_pair(expected_exit=0, expected_output=b'')
        self.assertEqual(counts, {'admitted': 0, 'completed': 0, 'live': 0, 'untracked': 0})

    def test_discarded_comma_result_does_not_create_a_record_lifetime(self):
        self.source.write_text('''#include <stdlib.h>
typedef struct WorkItem {int value;} WorkItem;
static WorkItem*work(void){return (malloc(32),malloc(sizeof(WorkItem)));}
int main(void){WorkItem*p=work();free(p);return 0;}
''')
        self.inventory = native_resources.inspect(self.policy, self.root, self.source,
                                                  self.folder / 'comma-inventory', tool=self.tool)
        counts = self.run_pair(expected_exit=0, expected_output=b'', extra=('-O0',))
        self.assertEqual(counts, {'admitted': 1, 'completed': 1, 'live': 0, 'untracked': 0})

    def test_conditional_condition_does_not_inherit_its_result_pointer_type(self):
        self.source.write_text('''#include <stdlib.h>
typedef struct WorkItem {int value;} WorkItem;
static WorkItem*work(void){return malloc(32)?malloc(sizeof(WorkItem)):NULL;}
int main(void){WorkItem*p=work();free(p);return 0;}
''')
        self.inventory = native_resources.inspect(self.policy, self.root, self.source,
                                                  self.folder / 'condition-inventory', tool=self.tool)
        counts = self.run_pair(expected_exit=0, expected_output=b'', extra=('-O0',))
        self.assertEqual(counts, {'admitted': 1, 'completed': 1, 'live': 0, 'untracked': 0})

    def test_constant_loops_switch_and_generic_keep_only_the_emitted_allocation(self):
        self.source.write_text('''#include <stdlib.h>
typedef struct WorkItem {int value;} WorkItem;
int main(void){
 while(0){WorkItem*p=malloc(sizeof *p);free(p);}
 for(int i=0;0;++i){WorkItem*p=malloc(sizeof *p);free(p);}
 switch(0){case 1:{WorkItem*p=malloc(sizeof *p);free(p);break;}default:break;}
 WorkItem*volatile p=_Generic(0,int:malloc(sizeof(WorkItem)),default:calloc(1,sizeof(WorkItem)));
 free(p);return 0;
}
''')
        self.inventory = native_resources.inspect(self.policy, self.root, self.source,
                                                  self.folder / 'constant-inventory', tool=self.tool)
        counts = self.run_pair(expected_exit=0, expected_output=b'', extra=('-O0',))
        self.assertEqual(counts, {'admitted': 1, 'completed': 1, 'live': 0, 'untracked': 0})

    def test_goto_retains_an_allocation_inside_a_constant_false_branch(self):
        self.source.write_text('''#include <stdlib.h>
typedef struct WorkItem {int value;} WorkItem;
int main(void){goto allocate;if(0){allocate:;WorkItem*volatile p=malloc(sizeof *p);free(p);}return 0;}
''')
        self.inventory = native_resources.inspect(self.policy, self.root, self.source,
                                                  self.folder / 'goto-inventory', tool=self.tool)
        counts = self.run_pair(expected_exit=0, expected_output=b'', extra=('-O0',))
        self.assertEqual(counts, {'admitted': 1, 'completed': 1, 'live': 0, 'untracked': 0})

    def test_inlined_source_site_accounts_for_each_remaining_allocation(self):
        self.source.write_text('''#include <stdlib.h>
#include <stdio.h>
typedef struct WorkItem {int value;} WorkItem;
static WorkItem*make(int value){
 WorkItem*p=malloc(sizeof *p);if(p)p->value=value;return p;
}
int main(void){
 WorkItem*volatile a=make(3);WorkItem*volatile b=make(4);
 if(!a||!b)return 2;int result=a->value+b->value;
 free(a);free(b);printf("%d\\n",result);return 0;
}
''')
        self.inventory = native_resources.inspect(self.policy, self.root, self.source,
                                                  self.folder / 'inline-inventory', tool=self.tool)
        counts = self.run_pair(expected_exit=0, expected_output=b'7\n')
        self.assertEqual(counts, {'admitted': 2, 'completed': 2, 'live': 0, 'untracked': 0})
        rows = json.loads((self.folder/'woven-resolution.json').read_text())['sites']
        allocations = [row for row in rows if row['callee'] == 'malloc']
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0]['caller'], 'make')
        self.assertEqual(allocations[0]['emitted_calls'], 2)

    def test_allocator_rewrite_stays_bound_to_its_original_source_site(self):
        self.source.write_text('''#include <stdlib.h>
#include <stdio.h>
#include <string.h>
typedef struct WorkItem {int value;char payload[4096];} WorkItem;
static WorkItem*make(void){
 WorkItem*p=malloc(sizeof *p);if(p)memset(p,0,sizeof *p);return p;
}
int main(void){
 WorkItem*volatile p=make();if(!p)return 2;
 int result=p->value;free(p);printf("%d\\n",result);return 0;
}
''')
        self.inventory = native_resources.inspect(self.policy, self.root, self.source,
                                                  self.folder / 'rewrite-inventory', tool=self.tool)
        counts = self.run_pair(expected_exit=0, expected_output=b'0\n')
        self.assertEqual(counts, {'admitted': 1, 'completed': 1, 'live': 0, 'untracked': 0})
        rows = json.loads((self.folder/'woven-resolution.json').read_text())['sites']
        self.assertEqual(sum(row['emitted_calls'] for row in rows if row['callee'] == 'malloc'), 1)
        allocation = next(row for row in rows if row['callee'] == 'malloc')
        self.assertEqual(allocation['emitted_callees'], ['calloc'])

    def test_resolution_output_is_exclusive(self):
        target = self.folder / 'existing-resolution.json'
        target.write_text('original')
        result = self.compile('existing')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'cannot create C resource resolution', result.stderr)
        self.assertEqual(target.read_text(), 'original')

    def test_incomplete_wrong_and_ambiguous_semantic_bindings_refuse(self):
        for name in ('schema', 'source', 'context', 'types', 'caller', 'location', 'standard', 'operation', 'duplicate'):
            value = copy.deepcopy(self.inventory)
            if name == 'schema': value['schema_version'] = 99
            elif name == 'source': value['source'] = str(self.root/'another.c')
            elif name == 'context': value.pop('compile_context')
            elif name == 'types': value['allocations'][0].pop('type_usr')
            elif name == 'caller': value['allocations'][0]['location']['debug_column'] += 1000
            elif name == 'location': value['allocations'][0]['location']['debug_line'] = 0
            elif name == 'standard': value['allocations'][0]['standard'] = False
            elif name == 'operation': value['allocations'][0]['callee'] = 'realloc'
            else: value['allocations'].append(copy.deepcopy(value['allocations'][0]))
            with self.subTest(name=name):
                result = self.compile(name, value=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'otelc:', result.stderr)
        self.assertNotEqual(self.compile('bad-json', raw='{broken').returncode, 0)

    def test_incompatible_application_probe_definitions_refuse(self):
        self.source.write_text(APP + 'void otelc_resource_allocated_v1(void*a,void*b,void*c){}\n')
        result = self.compile('conflict')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'C resource ABI', result.stderr)


if __name__ == '__main__': unittest.main()
