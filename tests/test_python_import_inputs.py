"""Real source and bytecode reads are retained without changing application IO."""

from __future__ import annotations

import os
import importlib.util
import py_compile
import subprocess
import sys
from pathlib import Path

from ci import build_python_inputs as tooling
from otelc import python_input_journal as journal
from python_input_support import NativeInputCase


class PythonImportInputTests(NativeInputCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        root = Path(__file__).resolve().parents[1]
        cls.faults = root / 'build/native' / ('python-import-fixture-' + sys.implementation.cache_tag)
        _, flags = tooling.toolchain()
        coverage = ['-fprofile-instr-generate', '-fcoverage-mapping'] if os.environ.get('OTELC_NATIVE_COVERAGE') else []
        subprocess.run([tooling.compiler(), '-std=c11', '-Wall', '-Wextra', '-Werror',
            *coverage, '-I' + str(root / 'native/python'), '-I' + str(cls.tool.parent),
            str(root / 'native/python/InputJournal.c'),
            str(root / 'native/python/FileIOCapture.c'),
            str(root / 'native/python/OriginLoader.c'),
            str(root / 'tests/fixtures/python_import_capture.c'), '-o', str(cls.faults), *flags], check=True)

    def observed(self, filename: Path, evidence: journal.InputJournal) -> list[bytes]:
        return [value.content for value in getattr(evidence, 'imports', ())
                if Path(os.fsdecode(value.path)) == filename]

    def run_original(self, source: str, *flags: str) -> journal.InputJournal:
        entry = self.root / 'entry.py'
        entry.write_text(source)
        arguments = [*flags, str(entry)]
        native = subprocess.run([sys.executable, *arguments], cwd=self.root,
                                capture_output=True, timeout=15)
        status, out, err, path, pid = self.launch(arguments)
        self.assertEqual((status, out, err), (native.returncode, native.stdout, native.stderr))
        self.assertEqual(status, 0, err)
        return journal.read(path, expected_pid=pid)

    def test_source_import_retains_consumed_bytes_after_application_rewrites_file(self) -> None:
        module = self.root / 'changing.py'
        content = b'import os\nprint("original module")\nos.unlink(__file__)\n'
        module.write_bytes(content)
        entry = self.root / 'entry.py'
        entry.write_text('import changing\n')
        status, out, err, path, pid = self.launch(['-B', str(entry)])
        self.assertEqual((status, out, err), (0, b'original module\n', b''))
        self.assertFalse(module.exists())
        evidence = journal.read(path, expected_pid=pid)
        self.assertTrue(evidence.complete)
        self.assertEqual(self.observed(module, evidence), [content])

    def test_unicode_symlink_and_bom_import_inputs_keep_actual_path_spelling(self) -> None:
        module = self.root / 'caf\u00e9.py'
        content = b'\xef\xbb\xbfvalue = 7\r\n'
        module.write_bytes(content)
        alias = self.root / 'linked.py'
        alias.symlink_to(module.name)
        evidence = self.run_original('import caf\u00e9, linked\nprint(caf\u00e9.value, linked.value, linked.__file__)\n', '-B')
        self.assertTrue(evidence.complete)
        self.assertEqual(self.observed(module, evidence), [content])
        self.assertEqual(self.observed(alias, evidence), [content])

    def test_unchecked_hash_cache_retains_whole_old_pyc_when_source_has_changed(self) -> None:
        module = self.root / 'cached.py'
        module.write_text('print("cached original")\nvalue = 7\n')
        bytecode = Path(py_compile.compile(str(module), doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH))
        original = bytecode.read_bytes()
        module.write_text('print("changed source")\nvalue = 8\n')
        evidence = self.run_original('import cached\nassert cached.value == 7\n', '-B')
        self.assertTrue(evidence.complete)
        self.assertEqual(self.observed(bytecode, evidence), [original])
        self.assertEqual(self.observed(module, evidence), [])
        self.assertEqual(original[:4], importlib.util.MAGIC_NUMBER)

    def test_invalid_pyc_header_and_source_fallback_are_distinct_full_reads(self) -> None:
        module = self.root / 'fallback.py'
        module.write_text('value = 7\n')
        bytecode = Path(py_compile.compile(str(module), doraise=True))
        content = bytecode.read_bytes()
        bytecode.write_bytes(b'BAD!' + content[4:])
        evidence = self.run_original('import fallback\nassert fallback.value == 7\n', '-B')
        self.assertTrue(evidence.complete)
        self.assertEqual(self.observed(bytecode, evidence), [bytecode.read_bytes()])
        self.assertEqual(self.observed(module, evidence), [module.read_bytes()])

    def test_sourceless_bytecode_and_package_inputs_preserve_native_metadata(self) -> None:
        package = self.root / 'pkg'
        package.mkdir()
        (package / '__init__.py').write_text('value = 10\n')
        module = package / 'child.py'
        module.write_text('value = 7\n')
        bytecode = Path(py_compile.compile(str(module), cfile=str(package / 'child.pyc'), doraise=True))
        module.unlink()
        evidence = self.run_original('''import pkg.child
print(pkg.value, pkg.child.value, pkg.child.__file__, pkg.child.__cached__,
      pkg.child.__package__, pkg.child.__spec__.name, type(pkg.child.__loader__).__name__)
''', '-B')
        self.assertTrue(evidence.complete)
        self.assertEqual(self.observed(bytecode, evidence), [bytecode.read_bytes()])
        self.assertEqual(self.observed(package / '__init__.py', evidence), [b'value = 10\n'])

    def test_explicit_open_code_and_spoofed_loader_filename_keep_real_file_semantics(self) -> None:
        data = self.root / 'data.py'
        data.write_bytes(b'abcdef')
        evidence = self.run_original('''import io, os
with io.open_code('data.py') as file:
    print(type(file).__name__, file.name, file.mode, os.fstat(file.fileno()).st_size,
          file.read(2), file.seek(1), file.read())
exec(compile("with io.open_code('data.py') as f: print(type(f).__name__, f.read())",
             '<frozen importlib._bootstrap_external>', 'exec'))
''', '-B')
        self.assertTrue(evidence.complete)
        self.assertEqual(self.observed(data, evidence), [])

    def test_custom_io_open_is_preserved_and_never_claims_a_complete_import(self) -> None:
        (self.root / 'module.py').write_text('value = 7\n')
        evidence = self.run_original('''import _io, importlib.machinery
original = _io.open
def custom(*args, **kwargs):
    print('custom open', args)
    return original(*args, **kwargs)
_io.open = custom
try:
    data = importlib.machinery.SourceFileLoader('module', 'module.py').get_data('module.py')
    print(data)
finally:
    _io.open = original
''', '-B')
        self.assertFalse(evidence.complete)
        self.assertGreaterEqual(evidence.capture_failures, 1)
        self.assertEqual(self.observed(self.root / 'module.py', evidence), [])

    def test_import_bound_and_nonregular_reads_keep_native_result_but_report_loss(self) -> None:
        module = self.root / 'large.py'
        module.write_bytes(b'value = 7\n#' + b'x' * journal.MAX_SOURCE)
        evidence = self.run_original('''import large, importlib.machinery
assert large.value == 7
print(importlib.machinery.SourceFileLoader('empty', '/dev/null').get_data('/dev/null'))
''', '-B')
        self.assertFalse(evidence.complete)
        self.assertGreaterEqual(evidence.capture_failures, 2)
        self.assertEqual(self.observed(module, evidence), [])

    def test_module_entry_collects_loader_reads_without_inventing_a_script_entry(self) -> None:
        module = self.root / 'main_module.py'
        content = b'print("module entry")\n'
        module.write_bytes(content)
        status, out, err, path, pid = self.launch(['-B', '-m', 'main_module'])
        self.assertEqual((status, out, err), (0, b'module entry\n', b''))
        evidence = journal.read(path, expected_pid=pid)
        self.assertFalse(evidence.complete)
        self.assertFalse(evidence.entry_expected)
        self.assertEqual(evidence.sources, ())
        self.assertEqual(self.observed(module, evidence), [content])

    def test_capture_faults_preserve_read_cleanup_and_allocation_failure_semantics(self) -> None:
        data = self.root / 'data.py'
        data.write_bytes(b'abc')
        path = self.root / 'faults.journal'
        with subprocess.Popen([str(self.faults), str(data), str(path), sys.executable],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            out, err = child.communicate(timeout=15)
        self.assertEqual((child.returncode, out, err), (0, b'', b''))
        evidence = journal.read(path, expected_pid=child.pid)
        self.assertGreaterEqual(evidence.capture_failures, 4)
        self.assertFalse(evidence.complete)
