"""Real source selections exclude library bodies and disambiguate native definitions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from otelc import llvm_backend as backend
from otelc.config import Policy
from tests.test_llvm_integration import no_core

ROOT = Path(__file__).resolve().parents[1]


class LLVMSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.keep = os.environ.get('OTELC_CPP_ANALYSIS') == '1'
        cls.temporary = tempfile.TemporaryDirectory(prefix='llvm-source-', dir=ROOT / 'build', delete=not cls.keep)
        cls.base = Path(cls.temporary.name).resolve()
        cls.app = cls.base / 'application'
        cls.app.mkdir()
        cls.plugin = backend.build(cls.base / 'plugin', coverage=True, function_registry=True, source_aware=True)
        cls.record = backend.verify(cls.plugin)
        cls.bin = Path(cls.record['toolchain']['directory'])
        subprocess.run([str(cls.bin / 'clang++'), '-std=c++17', '-DOTELC_FUNCTION_REGISTRY_V1=1',
                        '-I', str(backend.HEADER.parent), '-c', str(ROOT / 'tests/fixtures/llvm/hooks.cpp'),
                        '-o', str(cls.base / 'hooks.o')], check=True, capture_output=True, timeout=60)

    @classmethod
    def tearDownClass(cls) -> None:
        backend.verify(cls.plugin)
        output = ROOT / 'build'
        merged = output / 'llvm-pass-source.profdata'
        subprocess.run([str(cls.bin / 'llvm-profdata'), 'merge', '-sparse', *map(str, cls.base.glob('*.profraw')),
                        '-o', str(merged)], check=True, capture_output=True, timeout=60)
        command = [str(cls.bin / 'llvm-cov'), 'export', cls.record['plugin']['path'],
                   '-instr-profile=' + str(merged), str(backend.SOURCE)]
        data = json.loads(subprocess.check_output(command, timeout=60))
        files = [row for block in data['data'] for row in block['files']
                 if Path(row['filename']).resolve() == backend.SOURCE]
        assert len(files) == 1
        lcov = output / 'llvm-pass-source.lcov'
        lcov.write_bytes(subprocess.check_output([*command, '-format=lcov'], timeout=60))
        database = output / 'llvm-pass-source-compile-commands.json'
        shutil.copyfile(cls.base / 'plugin/compile_commands.json', database)
        (output / 'llvm-pass-source-coverage.json').write_text(json.dumps(data) + '\n')
        (output / 'llvm-pass-source-coverage-identity.json').write_text(json.dumps({
            'source': cls.record['source'], 'headers': cls.record['headers'],
            'lcov': backend.identity(lcov), 'compilation_database': backend.identity(database),
            'line_coverage': files[0]['summary']['lines']}, indent=2) + '\n')
        print('Source-aware LLVM coverage:', files[0]['summary']['lines'])
        assert files[0]['summary']['lines']['percent'] >= 80
        if not cls.keep:
            cls.temporary.cleanup()

    def compile(self, source: Path, *, includes: list[str] | None = None, excludes: list[str] | None = None,
                debug: bool = True, valid: bool = True, annotations: bool = False) -> Path:
        policy = Policy.parse({'schema_version': 3, 'languages': ['c', 'cpp'],
                              'sources': {'include': includes if includes is not None else ['**'], 'exclude': excludes or []},
                              'functions': {'include': ['*']}})
        env = backend.environment(policy, 'cpp', source_root=self.app)
        env.update(LLVM_PROFILE_FILE=str(self.base / '%m-%p.profraw'))
        if annotations:
            env['OTELC_READ_ANNOTATIONS'] = '1'
        output = source.with_suffix(source.suffix + '.o')
        compiler = self.bin / ('clang++' if source.suffix == '.cpp' else 'clang')
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        result = subprocess.run([str(compiler), '-O2', '-fexceptions', *(['-g'] if debug else []),
            '-fpass-plugin=' + self.record['plugin']['path'], '-c', str(source), '-o', str(output)],
            env=env, capture_output=True, timeout=90, preexec_fn=no_core)
        self.assertEqual(result.returncode == 0, valid, result.stderr.decode(errors='replace')[-2000:])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        return output

    def run_app(self, name: str, objects: list[Path], *, result: str) -> tuple[dict, list]:
        executable = self.base / name
        subprocess.run([str(self.bin / 'clang++'), *map(str, objects), str(self.base / 'hooks.o'),
                        '-pthread', '-o', str(executable)], check=True, capture_output=True, timeout=60)
        probe, registry = self.base / (name + '.json'), self.base / (name + '.names')
        env = dict(os.environ, PROBE_REPORT=str(probe), REGISTRY_REPORT=str(registry))
        actual = subprocess.run([str(executable)], env=env, capture_output=True, timeout=10)
        self.assertEqual(actual.returncode, 0, actual.stderr.decode())
        self.assertEqual(actual.stdout.decode(), result)
        counts, names = json.loads(probe.read_text()), registry.read_text().splitlines()
        self.assertEqual(counts['invalid'], 0)
        self.assertEqual(counts['entries'], counts['normal'] + counts['unwind'])
        return counts, names

    def test_library_exclusion_and_duplicate_file_local_names(self) -> None:
        a, b, main = [self.app / name for name in ('a.cpp', 'b.cpp', 'main.cpp')]
        for path, value in ((a, 7), (b, 8)):
            path.write_text('static __attribute__((noinline)) int helper(){return ' + str(value) + ';}\n'
                            + 'int ' + path.stem + '(){return helper();}\n')
        main.write_text('#include <string>\n#include <cstdio>\nint a();int b();\n'
                        'int main(){std::string value="original";printf("%s %d\\n",value.c_str(),a()+b());}\n')
        counts, names = self.run_app('duplicate', [self.compile(path) for path in (a, b, main)], result='original 15\n')
        self.assertEqual(set(names), {'a.cpp::helper()', 'a.cpp::a()', 'b.cpp::helper()', 'b.cpp::b()', 'main.cpp::main'})
        self.assertEqual(counts['entries'], 5)

    def test_path_globs_match_the_common_policy_with_unicode_and_exclusions(self) -> None:
        nested = self.app / 'include/deep'
        nested.mkdir(parents=True)
        header = nested / 'π.h'
        header.write_text('static __attribute__((noinline)) int selected_header(){return 42;}\n')
        source = self.app / 'policy.c'
        source.write_text('#include "include/deep/π.h"\n#include <stdio.h>\n'
                          'int main(){printf("%d\\n",selected_header());}\n')
        cases = [(['**/?.h'], [], True), (['include/*.h'], [], False),
                 (['include/**/*.h'], [], True), (['include/**'], ['**/?.h'], False),
                 (['**π.h'], [], True), (['**'], ['**/*.h'], False)]
        for index, (include, exclude, wanted) in enumerate(cases):
            with self.subTest(include=include, exclude=exclude):
                obj = self.compile(source, includes=include, excludes=exclude)
                counts, names = self.run_app('policy-' + str(index), [obj], result='42\n')
                expected = ['include/deep/π.h::selected_header'] if wanted else []
                if include == ['**']:
                    expected.append('policy.c::main')
                self.assertEqual(set(names), set(expected))
                self.assertEqual(counts['entries'], len(expected))

    def test_source_profile_requires_debug_metadata_and_canonical_root(self) -> None:
        source = self.app / 'no-debug.c'
        source.write_text('int main(){return 0;}\n')
        self.compile(source, debug=False, valid=False)
        policy = Policy.parse({'schema_version': 3})
        for root in (Path('/'), self.app / '..', self.app / 'missing'):
            with self.subTest(root=root), self.assertRaises(ValueError):
                backend.environment(policy, 'c', source_root=root)

    def test_a_project_symlink_does_not_select_a_foreign_header_definition(self) -> None:
        external = self.base / 'external.h'
        external.write_text('static __attribute__((noinline)) int external(){return 42;}\n')
        (self.app / 'inside.h').symlink_to(external)
        source = self.app / 'symlink.c'
        source.write_text('#include "inside.h"\n#include <stdio.h>\n'
                          'int main(){printf("%d\\n",external());}\n')
        counts, names = self.run_app('symlink', [self.compile(source)], result='42\n')
        self.assertEqual(names, ['symlink.c::main'])
        self.assertEqual(counts['entries'], 1)

    def test_exception_cleanup_and_annotation_exclusion_stay_balanced(self) -> None:
        source = self.app / 'exception.cpp'
        source.write_text('#include <stdexcept>\n#include <cstdio>\n'
            '__attribute__((annotate("otelc.exclude"),noinline)) int excluded(){return 5;}\n'
            '__attribute__((noinline)) void fail(){throw std::runtime_error("original");}\n'
            'int main(){try{fail();}catch(const std::runtime_error&e){printf("%s %d\\n",e.what(),excluded());}}\n')
        counts, names = self.run_app('exception', [self.compile(source, annotations=True)], result='original 5\n')
        self.assertEqual(set(names), {'exception.cpp::fail()', 'exception.cpp::main'})
        self.assertEqual((counts['entries'], counts['normal'], counts['unwind']), (2, 1, 1))
