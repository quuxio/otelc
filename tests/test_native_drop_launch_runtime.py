"""Tool-owned ABI harness against the real SDK, not automatic Rust weaving."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

from otelc import control, health
from test_java_integration import Capture

# This tooling process supplies explicit probes. Actual Rust bridge execution
# belongs to ci.rust_compiler_cases; no developer application imports ctypes.
HARNESS = '''import ctypes,os,sys
sdk=ctypes.CDLL(sys.argv[1],use_errno=True)
signatures={"otelc_native_start_v1":([ctypes.c_int],ctypes.c_int),
 "otelc_native_finish_v1":([],None),
 "otelc_function_named_enter_v1":([ctypes.c_char_p,ctypes.c_uint64],ctypes.c_uint64),
 "otelc_function_leave_v1":([ctypes.c_uint64,ctypes.c_int32],None),
 "otelc_object_created_v1":([ctypes.c_void_p,ctypes.c_char_p,ctypes.c_char_p],None),
 "otelc_object_relocated_v1":([ctypes.c_void_p,ctypes.c_void_p,ctypes.c_char_p],None),
 "otelc_object_destroying_v1":([ctypes.c_void_p,ctypes.c_char_p],ctypes.c_uint64),
 "otelc_object_destroyed_v1":([ctypes.c_uint64,ctypes.c_int32],None),
 "otelc_object_metadata_error_v1":([],None)}
for name,(arguments,result) in signatures.items():
 method=getattr(sdk,name);method.argtypes=arguments;method.restype=result
status=sdk.otelc_native_start_v1(int(sys.argv[2]))
if status:sys.exit(status)
print("ready",flush=True)
for index,line in enumerate(sys.stdin,1):
 command=int(line);ctypes.set_errno(17)
 if command==4:
  sdk.otelc_object_metadata_error_v1()
  sdk.otelc_object_relocated_v1(1,2,b"rust:private:Item")
 else:
  token=sdk.otelc_function_named_enter_v1(b"private-drop-action",19)
  source=index*2;destination=source+1
  sdk.otelc_object_created_v1(source,b"rust:private:Item",b"PrivateItem")
  sdk.otelc_object_relocated_v1(source,destination,b"rust:private:Item")
  if command!=3:
   item=sdk.otelc_object_destroying_v1(destination,b"rust:private:Item")
   assert item
   sdk.otelc_object_destroyed_v1(item,int(command==2))
  sdk.otelc_function_leave_v1(token,0)
 assert ctypes.get_errno()==17
 print(command,flush=True)
sdk.otelc_native_finish_v1()
'''


@unittest.skipUnless(os.environ.get('OTELC_NATIVE_LIBRARY'), 'select the genuine native launch SDK')
class NativeDropLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='otelc-private-drop-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.capture = Capture(); self.addCleanup(self.capture.close)
        self.library = Path(os.environ['OTELC_NATIVE_LIBRARY']).resolve(strict=True)

    def plan(self, folder: Path, *, collection: bool = True, emission: bool = True) -> dict:
        return {'schema_version': 3, 'language': 'rust', 'runtime': str(folder),
            'policy_sha256': 'a' * 64, 'selection_sha256': 'b' * 64, 'token': 'c' * 64,
            'settings': {'service_name': folder.name,
                'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                'interval_ms': 60000, 'timeout_ms': 1000, 'shutdown_timeout_ms': 5000,
                'collection': collection, 'emission': emission, 'traces': True, 'sample_ratio': 1,
                'boundaries': [1e-6, .001, 1.], 'drop_lifetimes': {'enabled': True, 'max_live': 16}}}

    def start(self, folder: Path, plan: dict) -> subprocess.Popen:
        folder.mkdir(mode=0o700)
        descriptor = os.open(folder / 'plan.json', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.write(descriptor, json.dumps(plan).encode())
            process = subprocess.Popen([sys.executable, '-I', '-B', '-c', HARNESS,
                str(self.library), str(descriptor)], pass_fds=(descriptor,),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        finally:
            os.close(descriptor)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def line(self, process: subprocess.Popen) -> bytes:
        self.assertTrue(select.select([process.stdout], [], [], 10)[0], 'private ABI response timed out')
        return process.stdout.readline()

    def call(self, process: subprocess.Popen, number: int) -> None:
        process.stdin.write(f'{number}\n'.encode()); process.stdin.flush()
        self.assertEqual(self.line(process), f'{number}\n'.encode())

    def test_all_startup_states_drop_unwind_censor_and_same_pid_live_controls(self) -> None:
        for collection, emission in ((False, False), (False, True), (True, False), (True, True)):
            folder = self.base / f'state-{int(collection)}{int(emission)}'
            process = self.start(folder, self.plan(folder, collection=collection, emission=emission))
            self.assertEqual(self.line(process), b'ready\n')
            endpoint = folder / 'control.json'; control.wait_ready(endpoint, process.pid)
            self.call(process, 1)
            control.request(endpoint, 'flush')
            control.request(endpoint, 'collection on'); control.request(endpoint, 'emission off')
            self.call(process, 2)
            before = control.request(endpoint, 'health')['metric_exports']['batches_started']
            control.request(endpoint, 'flush')
            self.assertEqual(control.request(endpoint, 'health')['metric_exports']['batches_started'], before)
            control.request(endpoint, 'collection off'); control.request(endpoint, 'emission on')
            self.call(process, 3)
            control.request(endpoint, 'on'); self.call(process, 1); self.call(process, 3)
            self.assertEqual(control.request(endpoint, 'status')['pid'], process.pid)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual((process.returncode, stdout, stderr), (0, b'', b''))
            final = health.read(folder / 'health.json')
            self.assertEqual((final['schema_version'], final['pid']), (5, process.pid))
            self.assertEqual(final['lifecycle']['shutdown'], 'succeeded')
            self.assertEqual(final['function_accounting']['metadata_errors'], '0')
            expected = dict(types='1', admitted='5', completed='3', live='0', dropping='0', ending='0',
                            censored='2', lost='0', duplicates='0', invalid='0', closing=True, closed=True)
            self.assertEqual(final['drop_accounting'], expected)
            spans = [span for request in self.capture.traces for resource in request.resource_spans
                     if any(item.key == 'service.name' and item.value.string_value == folder.name
                            for item in resource.resource.attributes)
                     for scope in resource.scope_spans for span in scope.spans]
            lifetimes = [span for span in spans if span.name == 'PrivateItem lifetime']
            self.assertEqual(len(spans), 10); self.assertEqual(len(lifetimes), 5)
            labels = [{item.key: item.value for item in span.attributes} for span in lifetimes]
            self.assertEqual(sum(row['boundary'].string_value == 'shutdown_censored' for row in labels), 2)
            completed = [row for row in labels if row['boundary'].string_value == 'drop_entry']
            self.assertEqual(sum(row['otelc.drop.exception_exit'].bool_value for row in completed), 1)
            self.assertTrue(all('otelc.destructor.exception_exit' not in row for row in labels))
            self.assertTrue(all(len(span.links) == 1 and not span.parent_span_id for span in lifetimes))

    def test_metadata_errors_and_wrong_profile_relocation_are_visible(self) -> None:
        folder = self.base / 'function-only'; plan = self.plan(folder); plan['schema_version'] = 2
        del plan['settings']['drop_lifetimes']
        process = self.start(folder, plan); self.assertEqual(self.line(process), b'ready\n')
        self.call(process, 4)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual((process.returncode, stdout, stderr), (0, b'', b''))
        final = health.read(folder / 'health.json')
        self.assertEqual(final['schema_version'], 1)
        self.assertNotIn('drop_accounting', final)
        self.assertEqual(final['function_accounting']['metadata_errors'], '2')

    def test_invalid_drop_plans_refuse_before_tool_actions(self) -> None:
        cases = [{'schema_version': value} for value in (1, 2, 3.0, True)]
        cases += [{'language': value} for value in ('c', 'cpp', 'java')]
        cases += [{'drop': value} for value in (None, {}, {'enabled': False, 'max_live': 1},
            {'enabled': 1, 'max_live': 1}, {'enabled': True, 'max_live': True},
            {'enabled': True, 'max_live': 0}, {'enabled': True, 'max_live': 1000001},
            {'enabled': True, 'max_live': 1, 'extra': 1})]
        cases += [{'foreign': key} for key in ('object_lifetimes', 'resource_lifetimes')]
        for index, change in enumerate(cases):
            folder = self.base / f'refusal-{index}'; plan = self.plan(folder)
            if 'drop' in change: plan['settings']['drop_lifetimes'] = change['drop']
            elif 'foreign' in change: plan['settings'][change['foreign']] = {'enabled': False, 'max_live': 1}
            else: plan.update(change)
            process = self.start(folder, plan)
            stdout, stderr = process.communicate(timeout=15)
            with self.subTest(change=change):
                self.assertEqual((process.returncode, stdout, stderr), (78, b'', b''))
                self.assertFalse((folder / 'control.json').exists())
                self.assertFalse((folder / 'health.json').exists())
