"""Real LLVM 22 hooks preserve ordinary C/C++ values, cleanup and selection."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import tempfile
import unittest

from otelc import llvm_backend as backend
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/llvm'


def no_core() -> None:
    """Expected compiler refusals must not produce large crash core files."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class LLVMIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='llvm-hooks-', dir=ROOT / 'build')
        cls.base = Path(cls.temporary.name).resolve()
        cls.plugin = backend.build(cls.base / 'plugin', coverage=True)
        cls.manifest = backend.verify(cls.plugin)
        cls.bin = Path(cls.manifest['toolchain']['directory'])
        cls.library = Path(cls.manifest['plugin']['path'])
        cls.original = {name.name: hashlib.sha256(name.read_bytes()).hexdigest() for name in FIXTURES.glob('*')}
        cls.policy = Policy.parse({'schema_version': 3, 'languages': ['c', 'cpp'],
                                  'functions': {'include': ['selected*', 'workload*']}})
        subprocess.run([str(cls.bin / 'clang++'), '-std=c++17', '-c', str(FIXTURES / 'hooks.cpp'),
                        '-o', str(cls.base / 'hooks.o')], check=True, capture_output=True, timeout=60)

    @classmethod
    def tearDownClass(cls) -> None:
        backend.verify(cls.plugin)
        assert cls.original == {name.name: hashlib.sha256(name.read_bytes()).hexdigest() for name in FIXTURES.glob('*')}
        profiles = sorted(cls.base.glob('*.profraw'))
        assert profiles, 'native pass produced no coverage profiles'
        merged = ROOT / 'build/llvm-pass.profdata'
        subprocess.run([str(cls.bin / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles),
                        '-o', str(merged)], check=True, capture_output=True, timeout=60)
        exported = subprocess.check_output([str(cls.bin / 'llvm-cov'), 'export', str(cls.library),
                                            '-instr-profile=' + str(merged), str(backend.SOURCE)], timeout=60)
        data = json.loads(exported)
        files = [file for block in data['data'] for file in block['files']
                 if Path(file['filename']).resolve() == backend.SOURCE.resolve()]
        assert len(files) == 1, 'native pass coverage is missing its exact source file'
        (ROOT / 'build/llvm-pass-coverage.json').write_bytes(exported)
        retained = ROOT / 'build' / cls.library.name
        shutil.copyfile(cls.library, retained)
        (ROOT / 'build/llvm-pass-coverage-identity.json').write_text(json.dumps({
            'plugin_sha256': cls.manifest['plugin']['sha256'], 'source': cls.manifest['source'],
            'line_coverage': files[0]['summary']['lines'], 'llvm_version': cls.manifest['toolchain']['version'],
            'scope': 'Exact compiler plugin executed inside Clang; fixture ABI counters are not a native SDK.'}, indent=2) + '\n')
        assert files[0]['summary']['lines']['percent'] >= 80, files[0]['summary']['lines']
        cls.temporary.cleanup()

    def run_command(self, args: list, *, environment: dict | None = None, success: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(list(map(str, args)), env=environment, capture_output=True,
                                timeout=90, preexec_fn=no_core)
        if success:
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace')[-4000:])
        else:
            self.assertNotEqual(result.returncode, 0, 'unsupported input was compiled successfully')
        return result

    def compiler_environment(self, language: str, policy: Policy | None = None) -> dict:
        environment = backend.environment(policy or self.policy, language)
        environment['LLVM_PROFILE_FILE'] = str(self.base / '%m-%p.profraw')
        return environment

    def compile(self, source: Path, name: str, language: str, *, policy: Policy | None = None,
                optimisation: str = '0', annotations: bool = False) -> tuple[Path, Path]:
        plain, woven, obj = (self.base / (name + suffix) for suffix in ('-plain', '-woven', '.o'))
        flags = ['-O' + optimisation] + (['-std=c++17', '-fexceptions', '-pthread'] if language == 'cpp' else [])
        compiler = self.bin / ('clang++' if language == 'cpp' else 'clang')
        self.run_command([compiler, *flags, source, '-o', plain])
        environment = self.compiler_environment(language, policy)
        if annotations:
            environment['OTELC_READ_ANNOTATIONS'] = '1'
        self.run_command([compiler, *flags, '-fpass-plugin=' + str(self.library), '-c', source, '-o', obj], environment=environment)
        self.run_command([self.bin / 'clang++', obj, self.base / 'hooks.o', '-pthread', '-o', woven])
        return plain, woven

    def compare(self, plain: Path, woven: Path, arguments: list[str], expected: dict) -> None:
        baseline = self.run_command([plain, *arguments])
        report = self.base / (woven.name + ('-thread' if arguments else '') + '.json')
        active = self.run_command([woven, *arguments], environment=dict(os.environ, PROBE_REPORT=str(report)))
        self.assertEqual((active.stdout, active.stderr), (baseline.stdout, baseline.stderr))
        self.assertEqual(json.loads(report.read_text()), expected)

    def test_normal_and_exception_exits_preserve_recursion_threads_and_cleanup(self) -> None:
        for optimisation in ('0', '2'):
            plain, woven = self.compile(FIXTURES / 'ordinary.c', 'c-' + optimisation, 'c', optimisation=optimisation)
            self.compare(plain, woven, [], {'entries': 2, 'normal': 2, 'unwind': 0, 'invalid': 0})
            plain, woven = self.compile(FIXTURES / 'exceptions.cpp', 'cpp-' + optimisation, 'cpp', optimisation=optimisation)
            for threaded in (False, True):
                factor = 2 if threaded else 1
                self.compare(plain, woven, ['thread'] if threaded else [],
                             {'entries': 13 * factor, 'normal': 6 * factor, 'unwind': 7 * factor, 'invalid': 0})

    def test_independent_ordinary_ir_example_uses_the_same_common_selection(self) -> None:
        source = ROOT / 'examples/llvm/workload.ll'
        before = source.read_bytes()
        plain, woven, ir, obj = (self.base / name for name in ('ir-plain', 'ir-woven', 'woven.ll', 'ir.o'))
        triple = self.run_command([self.bin / 'clang', '-dumpmachine']).stdout.decode().strip()
        self.run_command([self.bin / 'clang', source, '-o', plain])
        self.run_command([self.bin / 'opt', '-load-pass-plugin=' + str(self.library),
                          '-passes=default<O0>', '-mtriple=' + triple, '-S', source, '-o', ir],
                         environment=self.compiler_environment('c'))
        self.assertIn('call i64 @otelc_function_enter_v1', ir.read_text())
        self.run_command([self.bin / 'clang', '-c', ir, '-o', obj])
        self.run_command([self.bin / 'clang++', obj, self.base / 'hooks.o', '-o', woven])
        self.compare(plain, woven, [], {'entries': 1, 'normal': 1, 'unwind': 0, 'invalid': 0})
        self.assertEqual(source.read_bytes(), before)

    def test_common_question_mark_matches_one_unicode_codepoint(self) -> None:
        source = self.base / 'unicode.c'
        source.write_text('#include <stdio.h>\n__attribute__((noinline)) int π(void){return 42;}\n'
                          'int main(void){printf("%d\\n",π());return 0;}\n')
        policy = Policy.parse({'schema_version': 3, 'languages': ['c'], 'functions': {'include': ['?']}})
        self.assertTrue(policy.selection('functions').accepts('π'))
        plain, woven = self.compile(source, 'unicode', 'c', policy=policy)
        self.compare(plain, woven, [], {'entries': 1, 'normal': 1, 'unwind': 0, 'invalid': 0})

    def test_application_probe_definitions_refuse_before_emitting_an_object(self) -> None:
        source = self.base / 'collision.c'
        source.write_text('#include <stdint.h>\nuint64_t otelc_function_enter_v1(void *p){return 0;}\n'
                          'void otelc_function_leave_v1(uint64_t id,int32_t error){}\nint workload(void){return 42;}\n')
        output = self.base / 'collision.o'
        result = self.run_command([self.bin / 'clang', '-fpass-plugin=' + str(self.library),
                                   '-c', source, '-o', output], environment=self.compiler_environment('c'), success=False)
        self.assertIn(b'otelc:', result.stderr)
        self.assertFalse(output.exists())

    def test_common_exclusion_and_literal_cpp_punctuation(self) -> None:
        source = self.base / 'operators.cpp'
        source.write_text('#include <cstdio>\nstruct Box {__attribute__((noinline)) int operator[](int v){return v+1;}};\n'
                          '__attribute__((noinline)) int selectedA(){return 1;}\n'
                          '__attribute__((noinline)) int selectedB(){return 2;}\n'
                          'int main(){Box b;std::printf("%d\\n",b[38]+selectedA()+selectedB());}\n')
        policy = Policy.parse({'schema_version': 3, 'languages': ['cpp'],
                              'functions': {'include': ['Box::operator[](int)', 'selected?()'], 'exclude': ['selectedB()']}})
        plain, woven = self.compile(source, 'operators', 'cpp', policy=policy)
        self.compare(plain, woven, [], {'entries': 2, 'normal': 2, 'unwind': 0, 'invalid': 0})

    def test_existing_compiler_annotations_obey_exclusion_and_preserve_unrelated_attributes(self) -> None:
        source = self.base / 'annotations.c'
        source.write_text('#include <stdio.h>\n'
                          '__attribute__((noinline,annotate("otelc.instrument"),annotate("unrelated"))) int annotated(void){return 40;}\n'
                          '__attribute__((noinline,annotate("otelc.instrument"),annotate("otelc.exclude"))) int excluded(void){return 2;}\n'
                          'int main(void){printf("%d\\n",annotated()+excluded());return 0;}\n')
        policy = Policy.parse({'schema_version': 3, 'languages': ['c']})
        plain, woven = self.compile(source, 'annotations', 'c', policy=policy, annotations=True)
        self.compare(plain, woven, [], {'entries': 1, 'normal': 1, 'unwind': 0, 'invalid': 0})

    def test_operator_wildcard_matches_a_name_containing_an_asterisk(self) -> None:
        source = self.base / 'asterisk.cpp'
        source.write_text('#include <cstdio>\nstruct Box{__attribute__((noinline)) int operator*(){return 42;}};\n'
                          'int main(){Box b;std::printf("%d\\n",*b);}\n')
        policy = Policy.parse({'schema_version': 3, 'languages': ['cpp'],
                              'functions': {'include': ['Box::operator*']}})
        self.assertTrue(policy.selection('functions').accepts('Box::operator*()'))
        plain, woven = self.compile(source, 'asterisk', 'cpp', policy=policy)
        self.compare(plain, woven, [], {'entries': 1, 'normal': 1, 'unwind': 0, 'invalid': 0})

    def test_application_names_with_runtime_like_prefixes_follow_common_selection(self) -> None:
        source = self.base / 'application-prefixes.c'
        source.write_text('#include <stdio.h>\n'
                          '__attribute__((noinline)) int otelc_workload(void){return 20;}\n'
                          '__attribute__((noinline)) int __cyg_profile_func_workload(void){return 22;}\n'
                          'int main(void){printf("%d\\n",otelc_workload()+__cyg_profile_func_workload());return 0;}\n')
        names = ['otelc_workload', '__cyg_profile_func_workload']
        policy = Policy.parse({'schema_version': 3, 'languages': ['c'],
                              'functions': {'include': names}})
        for name in names:
            self.assertTrue(policy.selection('functions').accepts(name))
        plain, woven = self.compile(source, 'application-prefixes', 'c', policy=policy)
        self.compare(plain, woven, [], {'entries': 2, 'normal': 2, 'unwind': 0, 'invalid': 0})

    def test_matching_c_probe_declarations_remain_supported(self) -> None:
        source, output = self.base / 'compatible-abi.ll', self.base / 'compatible-abi-woven.ll'
        for attributes in ('', ' nounwind willreturn'):
            with self.subTest(attributes=attributes):
                source.write_text('declare i64 @otelc_function_enter_v1(ptr)' + attributes + '\n'
                                  'declare void @otelc_function_leave_v1(i64, i32)' + attributes + '\n'
                                  'define i32 @selected(){ret i32 42}\n')
                self.run_command([self.bin / 'opt', '-load-pass-plugin=' + str(self.library),
                                  '-passes=default<O0>', '-S', source, '-o', output],
                                 environment=self.compiler_environment('c'))
                self.assertIn('call i64 @otelc_function_enter_v1', output.read_text())
                self.assertIn('call void @otelc_function_leave_v1', output.read_text())

    def test_unsupported_ir_and_invalid_selection_refuse_before_output(self) -> None:
        cases = {
            'repeat': '!otelc.instrumented = !{}\ndefine void @selected(){ret void}\n',
            'musttail': 'declare i32 @other(i32)\ndefine i32 @selected(i32 %x){%r=musttail call i32 @other(i32 %x)\nret i32 %r}\n',
            'coroutine': 'declare void @llvm.coro.resume(ptr)\ndefine void @selected(){call void @llvm.coro.resume(ptr null)\nret void}\n',
            'personality': 'declare i32 @custom_personality(...)\ndefine void @selected() personality ptr @custom_personality {ret void}\n',
            'abi-global': '@otelc_function_enter_v1=global i64 0\ndefine void @selected(){ret void}\n',
            'abi-type': 'declare void @otelc_function_enter_v1()\ndefine void @selected(){ret void}\n',
            'abi-enter-convention': 'declare fastcc i64 @otelc_function_enter_v1(ptr)\ndefine void @selected(){ret void}\n',
            'abi-leave-convention': 'declare fastcc void @otelc_function_leave_v1(i64, i32)\ndefine void @selected(){ret void}\n',
            'abi-enter-memory': 'declare i64 @otelc_function_enter_v1(ptr) memory(none)\ndefine void @selected(){ret void}\n',
            'abi-leave-memory': 'declare void @otelc_function_leave_v1(i64, i32) memory(none)\ndefine void @selected(){ret void}\n',
            'abi-no-return': 'declare i64 @otelc_function_enter_v1(ptr) noreturn\ndefine void @selected(){ret void}\n',
            'abi-by-value': 'declare i64 @otelc_function_enter_v1(ptr byval(i64))\ndefine void @selected(){ret void}\n',
            'abi-return-attribute': 'declare inreg i64 @otelc_function_enter_v1(ptr)\ndefine void @selected(){ret void}\n',
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                source, output = self.base / (name + '.ll'), self.base / (name + '-out.ll')
                source.write_text(text)
                result = self.run_command([self.bin / 'opt', '-load-pass-plugin=' + str(self.library),
                                           '-passes=default<O0>', '-S', source, '-o', output],
                                          environment=self.compiler_environment('cpp'), success=False)
                self.assertIn(b'otelc:', result.stderr)
                self.assertFalse(output.exists())
        source = self.base / 'invalid-selection.c'
        source.write_text('int workload(void){return 42;}\n')
        for value in (None, '{', '{}', '[1]'):
            env = self.compiler_environment('c')
            if value is None:
                env.pop('OTELC_FUNCTION_INCLUDE')
            else:
                env['OTELC_FUNCTION_INCLUDE'] = value
            output = self.base / ('invalid-' + str(value) + '.o')
            result = self.run_command([self.bin / 'clang', '-fpass-plugin=' + str(self.library),
                                       '-c', source, '-o', output], environment=env, success=False)
            self.assertIn(b'otelc:', result.stderr)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
