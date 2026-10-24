"""Actual source-loader reads are linked through the original compiler return."""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import subprocess
import sys

from ci import build_python_inputs as tooling
from otelc import python_origin_journal as journal
from otelc.python_code_descriptor import encode
from python_input_support import NativeInputCase


class PythonLoaderOriginTests(NativeInputCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.tool = tooling.build(coverage=bool(os.environ.get('OTELC_NATIVE_COVERAGE')), origin_entry=True)

    def require_profile(self) -> None:
        value = json.loads((self.tool.parent / 'build.json').read_bytes())
        if not value['inputs']['origin_chain']['available']:
            self.skipTest('the selected native source image is unavailable')

    def application(self, body: str, *, source: bytes = b'def chosen(value):\n    return value*2+3\n',
                    environment: dict[str, str] | None = None, flags: tuple[str, ...] = ()
                    ) -> tuple[bytes, journal.OriginJournal]:
        self.require_profile()
        target = self.root / 'target.py';target.write_bytes(source)
        entry = self.root / 'original.py'
        entry.write_text("import _frozen_importlib_external as external\n"
                         "path=__file__.replace('original.py','target.py')\n" + body)
        arguments = ['-B', *flags, str(entry)]
        stock = subprocess.run([sys.executable, *arguments], env=environment, capture_output=True, timeout=15)
        status, out, err, path, pid = self.launch(arguments, environment=environment, loader_origins=True)
        self.assertEqual((status, out, err), (stock.returncode, stock.stdout, stock.stderr))
        self.assertEqual(target.read_bytes(), source)
        return out, journal.read(path, expected_pid=pid)

    def test_repeated_actual_reads_compiler_roots_and_executions(self) -> None:
        self.require_profile()
        target = self.root / 'target.py'
        data = b'def chosen(value):\n    return value*2+3\n'
        target.write_bytes(data)
        entry = self.root / 'original.py'
        entry.write_text('''import _frozen_importlib_external as external
loader=external.SourceFileLoader('target', __file__.replace('original.py','target.py'))
for index in range(3):
    code=loader.get_code('target')
    namespace={}
    exec(code,namespace)
    print(namespace['chosen'](7))
''')
        stock = subprocess.run([sys.executable, '-B', str(entry)], capture_output=True, timeout=15)
        status, out, err, path, pid = self.launch(['-B', str(entry)], loader_origins=True)
        self.assertEqual((status, out, err), (stock.returncode, stock.stdout, stock.stderr))
        self.assertEqual(out, b'17\n17\n17\n')
        value = journal.read(path, expected_pid=pid)
        self.assertTrue(value.capture_complete)
        roots = [root for root in value.roots if root.route == 2]
        self.assertEqual(len(roots), 3)
        self.assertEqual(len({root.input_sequence for root in roots}), 3)
        self.assertEqual(len({root.loader for root in roots}), 1)
        self.assertEqual(len(value.executions), 7)
        for root in roots:
            self.assertEqual((root.compiler_flags, root.optimize), (0, 0))
            self.assertEqual(root.descriptor.sha256, hashlib.sha256(encode(compile(
                data, str(target), 'exec', dont_inherit=True))).hexdigest())
        self.assertEqual(target.read_bytes(), data)

    def test_equal_byte_compilations_do_not_borrow_the_read_transaction(self) -> None:
        out, value = self.application('''loader=external.SourceFileLoader('target',path)
data=loader.get_data(path)
before=compile(data,path,'exec',dont_inherit=True)
code=loader.get_code('target')
after=compile(data,path,'exec',dont_inherit=True)
for current in (before,code,after):
    namespace={};exec(current,namespace);print(namespace['chosen'](7))
''')
        self.assertEqual(out, b'17\n17\n17\n')
        self.assertTrue(value.capture_complete)
        roots = [root for root in value.roots if root.route == 2]
        self.assertEqual(len(roots), 1)
        self.assertEqual(len([event for event in value.executions if event.root == roots[0].identity]), 2)

    def test_interpreter_optimization_uses_the_actual_compiler_level(self) -> None:
        source = b'"module docstring"\ndef chosen(value):\n    "function docstring"\n    return value+__debug__\n'
        for level, flags in ((0, ()), (1, ('-O',)), (2, ('-OO',))):
            with self.subTest(level=level):
                out, value = self.application("loader=external.SourceFileLoader('target',path)\n"
                    "code=loader.get_code('target');namespace={};exec(code,namespace);print(namespace['chosen'](7))\n",
                    source=source, flags=flags)
                self.assertEqual(out, b'8\n' if level == 0 else b'7\n')
                self.assertTrue(value.capture_complete)
                roots = [root for root in value.roots if root.route == 2]
                self.assertEqual(len(roots), 1)
                self.assertEqual(roots[0].optimize, level)
                expected = compile(source, str(self.root / 'target.py'), 'exec', dont_inherit=True, optimize=level)
                self.assertEqual(roots[0].descriptor.sha256, hashlib.sha256(encode(expected)).hexdigest())

    def test_source_encoding_cookie_keeps_the_actual_read_bytes(self) -> None:
        source = b"# coding: latin-1\ndef chosen(value):\n    return value+len('caf\xe9')\n"
        out, value = self.application("loader=external.SourceFileLoader('target',path)\n"
            "code=loader.get_code('target');namespace={};exec(code,namespace);print(namespace['chosen'](7))\n",
            source=source)
        self.assertEqual(out, b'11\n')
        self.assertTrue(value.capture_complete)
        roots = [root for root in value.roots if root.route == 2]
        self.assertEqual(len(roots), 1)
        expected = compile(source, str(self.root / 'target.py'), 'exec', dont_inherit=True)
        self.assertEqual(roots[0].descriptor.sha256, hashlib.sha256(encode(expected)).hexdigest())

    def test_replaced_instance_reads_or_compilers_cannot_supply_a_root(self) -> None:
        alternatives = [
            "loader.get_data=lambda filename:b'def chosen(value): return value*2+3\\n'",
            "loader.source_to_code=lambda data,filename:compile(b'def chosen(value): return 99\\n',filename,'exec')",
            "external.compile=lambda *args,**kwargs:compile(b'def chosen(value): return 99\\n',path,'exec')",
        ]
        for replacement in alternatives:
            with self.subTest(replacement=replacement):
                out, value = self.application("loader=external.SourceFileLoader('target',path)\n" + replacement +
                    "\ncode=loader.get_code('target')\nnamespace={};exec(code,namespace);print(namespace['chosen'](7))\n")
                self.assertIn(out, (b'17\n', b'99\n'))
                self.assertFalse(value.capture_complete)
                self.assertEqual([root for root in value.roots if root.route == 2], [])

    def test_failed_compiles_and_reads_preserve_exceptions_without_stale_transactions(self) -> None:
        out, value = self.application('''loader=external.SourceFileLoader('target',path)
for index in range(50):
    try:loader.get_code('target')
    except SyntaxError as error:
        assert error.filename==path
print('fifty original syntax errors')
''', source=b'def broken(:\n')
        self.assertEqual(out, b'fifty original syntax errors\n')
        self.assertTrue(value.capture_complete)
        self.assertEqual(len(value.roots), 1)

    def test_loader_and_code_weak_references_release_between_transactions(self) -> None:
        out, value = self.application('''import gc,_weakref
references=[]
for index in range(40):
    loader=external.SourceFileLoader('target',path)
    code=loader.get_code('target');namespace={};exec(code,namespace)
    assert namespace['chosen'](7)==17
    references.extend((_weakref.ref(loader),_weakref.ref(code),_weakref.ref(namespace['chosen'].__code__)))
    del namespace,code,loader
gc.collect()
print(sum(reference() is None for reference in references))
''')
        self.assertEqual(out, b'120\n')
        self.assertTrue(value.capture_complete)
        roots = [root for root in value.roots if root.route == 2]
        self.assertEqual(len(roots), 40)
        self.assertEqual(len({root.loader for root in roots}), 40)

    def test_actual_worker_transactions_use_distinct_read_and_root_identities(self) -> None:
        out, value = self.application('''import _thread
locks=[]
def work(done):
    loader=external.SourceFileLoader('target',path)
    code=loader.get_code('target');namespace={};exec(code,namespace)
    print(namespace['chosen'](7));done.release()
for index in range(8):
    done=_thread.allocate_lock();done.acquire();locks.append(done)
    _thread.start_new_thread(work,(done,))
for done in locks:done.acquire()
''')
        self.assertEqual(out, b'17\n' * 8)
        self.assertTrue(value.capture_complete)
        roots = [root for root in value.roots if root.route == 2]
        self.assertEqual(len(roots), 8)
        self.assertEqual(len({root.input_sequence for root in roots}), 8)

    def test_compiler_object_hash_and_existing_dictionary_keys_are_preserved(self) -> None:
        startup = self.root / 'sitecustomize.py'
        startup.write_text('import sys\nsys.compiler_keys={compile:hash(compile)}\n')
        out, value = self.application('''import sys
assert next(iter(sys.compiler_keys)) is compile
assert sys.compiler_keys[compile]==hash(compile)
loader=external.SourceFileLoader('target',path)
code=loader.get_code('target');namespace={};exec(code,namespace)
assert sys.compiler_keys[compile]==hash(compile)
print(namespace['chosen'](7))
''', environment={**os.environ, 'PYTHONPATH': str(self.root)})
        self.assertEqual(out, b'17\n')
        self.assertTrue(value.capture_complete)

    def test_cached_bytecode_cannot_borrow_a_hash_checked_source_read(self) -> None:
        target = self.root / 'target.py'
        target.write_bytes(b'def chosen(value):\n    return value*2+3\n')
        py_compile.compile(str(target), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
        out, value = self.application("loader=external.SourceFileLoader('target',path)\n"
            "code=loader.get_code('target');namespace={};exec(code,namespace);print(namespace['chosen'](7))\n")
        self.assertEqual(out, b'17\n')
        self.assertFalse(value.capture_complete)
        self.assertEqual([root for root in value.roots if root.route == 2], [])

    def test_failure_before_compilation_does_not_retain_an_application_frame(self) -> None:
        out, value = self.application('''import gc,_weakref
def refuse(data,filename):raise ValueError('original compiler refusal')
loader=external.SourceFileLoader('target',path)
reference=_weakref.ref(loader)
loader.source_to_code=refuse
try:loader.get_code('target')
except ValueError:pass
del loader
gc.collect()
print(reference() is None)
loader=external.SourceFileLoader('target',path)
code=loader.get_code('target');namespace={};exec(code,namespace)
print(namespace['chosen'](7))
''')
        self.assertEqual(out, b'True\n17\n')
        self.assertTrue(value.capture_complete)
        self.assertEqual(len([root for root in value.roots if root.route == 2]), 1)
