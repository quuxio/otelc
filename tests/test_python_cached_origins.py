"""Actual cache reads bind original unmarshalling and loader returns, not recompilation."""
from __future__ import annotations
import json
import marshal
import os
from pathlib import Path
import py_compile
import subprocess
import sys

from ci import build_python_inputs as tooling
from otelc import python_origin_journal as journal
from python_input_support import NativeInputCase
from test_java_integration import Capture


class PythonCachedOriginTests(NativeInputCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.tool = tooling.build(coverage=bool(os.environ.get('OTELC_NATIVE_COVERAGE')), origin_entry=True)

    def setUp(self) -> None:
        super().setUp()
        inputs = json.loads((self.tool.parent / 'build.json').read_bytes())['inputs']
        if not inputs['origin_chain']['available']:
            self.skipTest('the selected native origin image is unavailable')
        self.app = self.root / 'application'
        self.app.mkdir()
        self.target = self.app / 'target.py'
        self.target.write_text('def chosen(value): return value*2+3\n')
        self.entry = self.app / 'entry.py'

    def run_cache(self, body: str = '', *, level: int = 0,
                  mode: py_compile.PycInvalidationMode = py_compile.PycInvalidationMode.TIMESTAMP,
                  prefix: str = '', output: bytes = b'17\n', cache_edit: str | None = None,
                  environment: dict[str, str] | None = None) -> journal.OriginJournal:
        cache = Path(py_compile.compile(str(self.target), doraise=True, optimize=level,
                                       invalidation_mode=mode))
        source = self.target.read_bytes()
        if cache_edit is not None:
            data = cache.read_bytes()
            cache.write_bytes({'magic': b'\0'*4 + data[4:], 'timestamp': data[:8] + b'\0'*8 + data[16:],
                               'payload': data[:16] + b'\0',
                               'function': data[:16] + marshal.dumps(compile(source, str(self.target), 'exec').co_consts[0])}[cache_edit])
        cached = cache.read_bytes()
        source = self.target.read_bytes()
        self.entry.write_text("import _frozen_importlib_external as external\n" + prefix +
            "\npath=__file__.replace('entry.py','target.py')\n" + (body or
            "loader=external.SourceFileLoader('target',path)\n"
            "code=loader.get_code('target');namespace={};exec(code,namespace);print(namespace['chosen'](7))\n"))
        arguments = ['-B', *(['-O' if level == 1 else '-OO'] if level else []), str(self.entry)]
        stock = subprocess.run([sys.executable, *arguments], capture_output=True, timeout=20, env=environment)
        result, out, err, path, pid = self.launch(arguments, origin_root=self.app, cache_origins=True, environment=environment)
        self.assertEqual((result, out, err), (stock.returncode, stock.stdout, stock.stderr))
        self.assertEqual(out, output)
        self.assertEqual(self.target.read_bytes(), source)
        self.assertEqual(cache.read_bytes(), cached)
        with self.assertRaises(ValueError):
            journal.read(path, expected_pid=pid)
        self.last_journal, self.last_pid = path, pid
        return journal.read(path, expected_pid=pid, version=3)

    def test_original_valid_cache_has_its_own_read_and_deserialisation_root(self) -> None:
        value = self.run_cache()
        self.assertTrue(value.capture_complete)
        roots = [root for root in value.roots if root.route == 3]
        self.assertEqual(len(roots), 1)
        self.assertEqual((roots[0].compiler_flags, roots[0].optimize), (0, 3))
        self.assertTrue(roots[0].descriptor.path.endswith(b'target.py'))
        read = dict(value.imports)[roots[0].input_sequence]
        self.assertTrue(read.path.endswith(b'.pyc'))
        self.assertEqual(len([item for item in value.executions if item.root == roots[0].identity]), 2)
        self.assertTrue(all(item.path.endswith(b'target.py') for item in value.executions
                            if item.root == roots[0].identity))

    def test_original_cache_modes_and_optimization_are_not_changed(self) -> None:
        for mode in py_compile.PycInvalidationMode:
            for level in (0, 1, 2):
                with self.subTest(mode=mode, level=level):
                    value = self.run_cache(level=level, mode=mode)
                    self.assertTrue(value.capture_complete)
                    self.assertEqual(len([root for root in value.roots if root.route == 3]), 1)

    def test_rejected_headers_fall_back_to_the_original_source_compiler(self) -> None:
        for edit in ('magic', 'timestamp'):
            with self.subTest(edit=edit):
                value = self.run_cache(cache_edit=edit)
                self.assertTrue(value.capture_complete)
                self.assertEqual([root.route for root in value.roots], [1, 2])

    def test_original_marshal_failure_keeps_the_original_caught_exception(self) -> None:
        value = self.run_cache('''loader=external.SourceFileLoader('target',path)
try: loader.get_code('target')
except (ValueError,EOFError) as error: print(type(error).__name__)
''', cache_edit='payload', output=b'ValueError\n')
        self.assertTrue(value.capture_complete)
        self.assertEqual([root.route for root in value.roots], [1])

    def test_repeated_warm_calls_have_distinct_reads_roots_and_weak_lifetimes(self) -> None:
        value = self.run_cache('''import gc,_weakref
references=[]
for index in range(40):
    loader=external.SourceFileLoader('target',path)
    code=loader.get_code('target');namespace={};exec(code,namespace)
    assert namespace['chosen'](7)==17
    references.extend((_weakref.ref(loader),_weakref.ref(code),_weakref.ref(namespace['chosen'].__code__)))
    del namespace,code,loader
gc.collect()
print(sum(reference() is None for reference in references))
''', output=b'120\n')
        self.assertTrue(value.capture_complete)
        roots = [root for root in value.roots if root.route == 3]
        self.assertEqual(len(roots), 40)
        self.assertEqual(len({root.input_sequence for root in roots}), 40)
        self.assertEqual(len({root.loader for root in roots}), 40)

    def test_existing_marshal_keys_and_builtin_identity_metadata_are_preserved(self) -> None:
        startup = self.root / 'sitecustomize.py'
        startup.write_text('import sys,marshal\nsys.original_loads=marshal.loads\nsys.loads_keys={marshal.loads:hash(marshal.loads)}\n')
        value = self.run_cache(prefix='''import sys,marshal,inspect
assert sys.original_loads is marshal.loads
assert sys.loads_keys[marshal.loads]==hash(marshal.loads)
assert marshal.loads.__name__=='loads' and inspect.isbuiltin(marshal.loads)
''', environment={**os.environ, 'PYTHONPATH': str(self.root)})
        self.assertTrue(value.capture_complete)
        self.assertEqual(len(value.roots), 2)

    def test_worker_cache_transactions_use_distinct_actual_read_tokens(self) -> None:
        value = self.run_cache('''import _thread
locks=[]
def work(done):
    loader=external.SourceFileLoader('target',path)
    code=loader.get_code('target');namespace={};exec(code,namespace)
    print(namespace['chosen'](7));done.release()
for index in range(8):
    done=_thread.allocate_lock();done.acquire();locks.append(done)
    _thread.start_new_thread(work,(done,))
for done in locks: done.acquire()
''', output=b'17\n'*8)
        self.assertTrue(value.capture_complete)
        roots = [root for root in value.roots if root.route == 3]
        self.assertEqual(len(roots), 8)
        self.assertEqual(len({root.input_sequence for root in roots}), 8)

    def test_substituted_marshal_helpers_or_equal_foreign_views_cannot_claim_origin(self) -> None:
        replacements = (
            'original=marshal.loads;marshal.loads=lambda data:original(data)',
            'original=external._compile_bytecode;external._compile_bytecode=lambda *args,**kwargs:original(*args,**kwargs)',
            'external.memoryview=lambda data:memoryview(bytes(bytearray(data)))',
            'external.memoryview=lambda data:memoryview(bytearray(data))',
            'external.memoryview=lambda data:memoryview(b"X"+data)[1:]',
            'original=_imp._fix_co_filename;_imp._fix_co_filename=lambda *args:original(*args)',
        )
        for changed in replacements:
            with self.subTest(changed=changed):
                value = self.run_cache(prefix='import marshal,_imp\n'+changed+'\n')
                self.assertFalse(value.capture_complete)
                self.assertEqual([root.route for root in value.roots], [1])

    def test_unrelated_equal_cache_loads_do_not_acquire_the_loader_transaction(self) -> None:
        value = self.run_cache('''import marshal
cached=open(external.cache_from_source(path),'rb').read()
unrelated=marshal.loads(cached[16:])
namespace={};exec(unrelated,namespace);print(namespace['chosen'](7))
loader=external.SourceFileLoader('target',path)
code=loader.get_code('target');namespace={};exec(code,namespace);print(namespace['chosen'](7))
''', output=b'17\n17\n')
        self.assertTrue(value.capture_complete)
        roots = [root for root in value.roots if root.route == 3]
        self.assertEqual(len(roots), 1)
        self.assertEqual(len([item for item in value.executions if item.root == roots[0].identity]), 2)

    def test_nonmodule_cache_code_cannot_be_published_as_a_module_origin(self) -> None:
        value = self.run_cache('''loader=external.SourceFileLoader('target',path)
try: exec(loader.get_code('target'),{})
except TypeError: print('TypeError')
''', cache_edit='function', output=b'TypeError\n')
        self.assertFalse(value.capture_complete)
        self.assertEqual([root.route for root in value.roots], [1])

    def test_cache_wire_version_and_input_route_cannot_be_substituted(self) -> None:
        value = self.run_cache()
        self.assertTrue(value.capture_complete)
        raw = self.last_journal.read_bytes()
        offset = 0
        while offset < len(raw):
            _, kind, _, _, _, path_size, size = journal.inputs.HEADER.unpack_from(raw, offset)
            start = offset + journal.inputs.HEADER.size + path_size
            if kind == 5:
                fields = journal.ROOT.unpack_from(raw, start)
                if fields[5] == 3:
                    break
            offset = start + size
        else:
            self.fail('actual cache root missing from the maintained fixture')
        for field, replacement in ((3, 0), (4, value.sources[0][0]), (5, 99), (8, 1), (9, 0), (9, 2)):
            changed = list(fields)
            changed[field] = replacement
            candidate = raw[:start] + journal.ROOT.pack(*changed) + raw[start+journal.ROOT.size:]
            with self.subTest(field=field), self.assertRaises(ValueError):
                journal.decode(candidate, expected_pid=self.last_pid, version=3)
        for version in (2, True, 4, None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                journal.decode(raw, expected_pid=self.last_pid, version=version)

    def test_actual_sdk_monitor_joins_cached_main_and_worker_calls(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        cache = Path(py_compile.compile(str(self.target), doraise=True))
        cached, source = cache.read_bytes(), self.target.read_bytes()
        repository = Path(__file__).resolve().parents[1]
        # Internal external-bootstrap fixture; target.py remains the ordinary application.
        self.entry.write_text(f'''import sys
sys.path.insert(0,{str(repository)!r})
import threading
from pathlib import Path
from otelc.config import Policy
from otelc.python_monitor import PythonMonitor
from otelc.python_runtime import PythonRuntime
from otelc.python_sdk_process import SdkProcess
policy=Policy.parse({{'schema_version':3,'languages':['python'],
    'sources':{{'include':['target.py']}},'functions':{{'include':['target.chosen']}},
    'metrics':{{'collection':True,'emission':True}},'traces':{{'enabled':True,'sample_ratio':1.0}},
    'export':{{'endpoint':'http://127.0.0.1:{capture.server.server_port}','interval_ms':100000,'timeout_ms':1000}}}})
root=Path(__file__).parent
sdk=SdkProcess.start(policy,root/'runtime')
runtime=PythonRuntime(policy,sdk.channel)
monitor=PythonMonitor(policy,root,runtime)
try:
    monitor.install()
    from target import chosen
    assert chosen(7)==17
    values=[]
    worker=threading.Thread(target=lambda:values.append(chosen(8)))
    worker.start();worker.join()
    assert values==[19]
finally:
    monitor.close();runtime.close()
report=sdk.wait_report()
assert report['phase']=='closed' and report['producer']['function_completions']==2
print('original cached values and clean SDK drain')
''')
        status, out, err, path, pid = self.launch(['-B', str(self.entry)], origin_root=self.app, cache_origins=True)
        self.assertEqual((status, out, err), (0, b'original cached values and clean SDK drain\n', b''))
        value = journal.read(path, expected_pid=pid, version=3)
        self.assertTrue(value.capture_complete)
        self.assertEqual(len([root for root in value.roots if root.route == 3]), 1)
        self.assertEqual(len(value.sdk_joins), 1)
        identity = value.sdk_joins[0][0]
        spans = [span for request in capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans), 2)
        for span in spans:
            self.assertEqual(span.name, 'target.chosen')
            self.assertEqual({item.key:item.value for item in span.attributes}['otelc.python.code_id'].int_value, identity)
        self.assertEqual(capture.calls(), 2)
        for name in ('otelc.function.calls','otelc.function.duration'):
            points = capture.points(name)
            self.assertTrue(points)
            for point in points:
                self.assertEqual({item.key:item.value for item in point.attributes}['otelc.python.code_id'].int_value, identity)
        self.assertEqual((cache.read_bytes(),self.target.read_bytes()),(cached,source))

    def test_incomplete_or_invalid_cache_root_options_never_run_the_entry(self) -> None:
        for flag in ('--origin-loader-caches-root', '--origin-loader-root'):
            child = subprocess.run([str(self.tool), flag], capture_output=True, timeout=10)
            self.assertEqual((child.returncode, child.stdout, child.stderr), (64, b'', b''))
        self.entry.write_text("raise AssertionError('entry must not run')\n")
        for root in (Path('.'), self.root / 'missing', self.target):
            with self.subTest(root=root):
                status, out, err, _, _ = self.launch(['-B', str(self.entry)], origin_root=root, cache_origins=True)
                self.assertEqual((status, out, err), (64, b'', b''))

    def test_refused_local_call_registration_preserves_app_and_reports_incomplete_capture(self) -> None:
        startup = self.root / 'sitecustomize.py'
        startup.write_text('''import sys
def audit(event,args):
    if event=='sys.monitoring.register_callback' and args[0] is not None and args[0].__name__=='cache_call':
        raise RuntimeError('application audit policy refused this monitoring callback')
sys.addaudithook(audit)
''')
        value = self.run_cache(environment={**os.environ,'PYTHONPATH':str(self.root)})
        self.assertFalse(value.capture_complete)
        self.assertGreater(value.capture_failures, 0)
        self.assertEqual(value.roots, ())

    def test_cached_encoding_cookie_preserves_original_source_and_value(self) -> None:
        self.target.write_bytes(b"# coding: latin-1\ndef chosen(value): return value+len('caf\xe9')\n")
        value = self.run_cache(output=b'11\n')
        self.assertTrue(value.capture_complete)
        self.assertEqual(len([root for root in value.roots if root.route == 3]), 1)

    def test_cache_monitor_keeps_global_and_unrelated_local_events_disabled(self) -> None:
        value = self.run_cache(prefix='''import sys
tools=[tool for tool in (2,5,3,4) if sys.monitoring.get_tool(tool)=='quux.otelc.origins']
if tools:
    tool=tools[0]
    assert sys.monitoring.get_events(tool)==0
    assert sys.monitoring.get_local_events(tool,external._code_to_timestamp_pyc.__code__)==0
    assert sys.monitoring.get_local_events(tool,external._compile_bytecode.__code__)==sys.monitoring.events.CALL
''')
        self.assertTrue(value.capture_complete)
