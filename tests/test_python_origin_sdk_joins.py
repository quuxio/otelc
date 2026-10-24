"""Native SDK starts join actual code identities to the source origin catalogue."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ci import build_python_inputs as tooling
from otelc import python_origin_journal as journal
from python_input_support import NativeInputCase
from test_python_code_inputs import FINISH, SETUP
from test_java_integration import Capture


class PythonOriginSdkJoinTests(NativeInputCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.tool = tooling.build(coverage=bool(os.environ.get('OTELC_NATIVE_COVERAGE')), origin_entry=True)

    def run_join(self, body: str, *, occupied: bool = False) -> tuple[list, journal.OriginJournal]:
        metadata = json.loads((self.tool.parent / 'build.json').read_bytes())
        if not metadata['inputs']['origin_chain']['available']:
            self.skipTest('the selected native source image is unavailable')
        environment = None
        if occupied:
            (self.root / 'sitecustomize.py').write_text("import sys\nsys.monitoring.use_tool_id(2,'application-tool')\n")
            environment = {**os.environ, 'PYTHONPATH': str(self.root)}
        entry = self.root / 'original.py'
        entry.write_text("NAMES=('chosen',)\n" + SETUP + body + FINISH + '\nimport json;print(json.dumps(identities))\n')
        status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True, environment=environment)
        self.assertEqual((status, err), (0, b''), out)
        self.frames = list(journal._frames(path.read_bytes()))
        return json.loads(out), journal.read(path, expected_pid=pid)

    def test_repeated_sdk_starts_have_one_identity_join_in_either_callback_order(self) -> None:
        orders = set()
        for occupied in (False, True):
            with self.subTest(occupied=occupied):
                identities, value = self.run_join('''
def chosen(value):return value*2+3
assert chosen(7)==17 and chosen(8)==19
''', occupied=occupied)
                self.assertEqual(identities, [1, 1])
                self.assertTrue(value.capture_complete)
                self.assertEqual(len(value.codes), 1)
                self.assertEqual(len(value.sdk_joins), 1)
                sdk, origin = value.sdk_joins[0]
                self.assertEqual(sdk, value.codes[0].identity)
                self.assertIn(origin, {item.identity for item in value.executions})
                code_sequence = next(sequence for kind, sequence, _, _, _ in self.frames if kind == 4)
                origin_sequence = next(sequence for kind, sequence, _, payload, _ in self.frames
                                       if kind == 6 and journal.EXECUTION.unpack_from(payload)[1] == origin)
                orders.add(origin_sequence < code_sequence)
        self.assertEqual(orders, {False, True})

    def test_independently_compiled_equal_code_cannot_borrow_an_origin_join(self) -> None:
        identities, value = self.run_join('''
def chosen(value):return value*2+3
assert chosen(7)==17
namespace={}
exec(compile('def chosen(value):return value*2+3\\n',__file__,'exec'),namespace)
assert namespace['chosen'](7)==17
''')
        self.assertEqual(identities, [1, None])
        self.assertFalse(value.capture_complete)
        self.assertGreater(value.capture_failures, 0)
        self.assertEqual(len(value.sdk_joins), 1)

    def test_manual_callback_cannot_create_a_second_join(self) -> None:
        identities, value = self.run_join('''
def chosen(value):return value*2+3
assert chosen(7)==17
observer(chosen.__code__,0)
''')
        self.assertEqual(identities, [1, None])
        self.assertFalse(value.capture_complete)
        self.assertEqual(len(value.sdk_joins), 1)

    def test_late_generator_resume_refuses_a_different_first_execution_offset(self) -> None:
        identities, value = self.run_join('''
sys.monitoring.set_events(3,0)
def chosen():
    yield 1
    yield 2
generator=chosen()
assert next(generator)==1
sys.monitoring.register_callback(3,sys.monitoring.events.PY_RESUME,observer)
sys.monitoring.set_events(3,sys.monitoring.events.PY_RESUME)
assert next(generator)==2
sys.monitoring.set_events(3,0)
sys.monitoring.register_callback(3,sys.monitoring.events.PY_RESUME,None)
generator.close()
''')
        self.assertEqual(identities, [None])
        self.assertFalse(value.capture_complete)
        self.assertEqual(value.sdk_joins, ())

    def test_actual_sdk_monitor_exports_the_joined_code_id_on_main_and_worker(self) -> None:
        metadata = json.loads((self.tool.parent / 'build.json').read_bytes())
        if not metadata['inputs']['origin_chain']['available']:
            self.skipTest('the selected native source image is unavailable')
        capture = Capture()
        self.addCleanup(capture.close)
        entry = self.root / 'original.py'
        repository = Path(__file__).resolve().parents[1]
        entry.write_text(f'''import sys
sys.path.insert(0,{str(repository)!r})
import json,threading
from pathlib import Path
from otelc.config import Policy
from otelc.python_monitor import PythonMonitor
from otelc.python_runtime import PythonRuntime
from otelc.python_sdk_process import SdkProcess
def chosen(value):return value*2+3
policy=Policy.parse({{'schema_version':3,'languages':['python'],
    'sources':{{'include':['*.py']}},'functions':{{'include':['original.chosen']}},
    'metrics':{{'collection':True,'emission':True}},'traces':{{'enabled':True,'sample_ratio':1.0}},
    'export':{{'endpoint':'http://127.0.0.1:{capture.server.server_port}','interval_ms':100000,'timeout_ms':1000}}}})
root=Path(__file__).parent
sdk=SdkProcess.start(policy,root/'runtime')
runtime=PythonRuntime(policy,sdk.channel)
monitor=PythonMonitor(policy,root,runtime)
try:
    monitor.install()
    assert chosen(7)==17
    values=[]
    worker=threading.Thread(target=lambda:values.append(chosen(8)))
    worker.start();worker.join()
    assert values==[19]
finally:
    monitor.close();runtime.close()
report=sdk.wait_report()
assert report['phase']=='closed' and report['producer']['function_completions']==2
print('original values and clean SDK drain')
''')
        status, out, err, path, pid = self.launch(['-B', str(entry)], entry_origins=True)
        self.assertEqual((status, out, err), (0, b'original values and clean SDK drain\n', b''))
        value = journal.read(path, expected_pid=pid)
        self.assertTrue(value.capture_complete)
        self.assertEqual(len(value.sdk_joins), 1)
        code_id = value.sdk_joins[0][0]
        spans = [span for request in capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans), 2)
        for span in spans:
            self.assertEqual(span.name, 'original.chosen')
            attributes = {item.key: item.value for item in span.attributes}
            self.assertEqual(attributes['otelc.python.code_id'].int_value, code_id)
        self.assertEqual(capture.calls(), 2)
        for name in ('otelc.function.calls', 'otelc.function.duration'):
            points = capture.points(name)
            self.assertTrue(points)
            for point in points:
                attributes = {item.key: item.value for item in point.attributes}
                self.assertEqual(attributes['otelc.python.code_id'].int_value, code_id)
