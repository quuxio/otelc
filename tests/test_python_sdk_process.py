"""Real SDK workers preserve native fixture channels and drain outside the app.

These fixtures deliberately call the internal bridge API. They do not claim
source-free application instrumentation, which is a separate frontend task.
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from otelc.config import Policy
from otelc.python_sdk_process import SdkProcess
from test_java_integration import Capture
from test_python_sdk_protocol import context, span


APPLICATION = '''import json,os,random,sys,time
from otelc.python_sdk_process import SdkChannel
random.seed(12345);before=random.getstate()
channel=SdkChannel(int(sys.argv[1]),int(sys.argv[2]),sdk_pid=int(sys.argv[3]),
 launch_id=sys.argv[4],policy_sha256=sys.argv[5])
channel.bind(os.getpid())
for index,(collection,emission) in enumerate(((False,False),(True,False),(False,True),(True,True))):
 channel.controls(collection=collection,emission=emission)
 assert channel.writer.send({'kind':'metric','scope':'fixture','instrument':'counter',
  'name':'otelc.function.calls','value':1,'attributes':{'function':'workload'}})
 started=time.time_ns()
 assert channel.writer.send({'kind':'span','span':{'name':'fixture.workload',
  'context':{'trace_id':f'{index+1:032x}','span_id':f'{index+1:016x}',
             'trace_flags':1,'remote':False},
  'start_time':started,'end_time':started+1000,
  'attributes':{'state':index,'unicode':'α😀'},
  'events':[{'name':'event','timestamp':started+1,'attributes':{'count':1},
             'dropped_attributes':2}],
  'links':[{'context':{'trace_id':'4'.zfill(32),'span_id':'5'.zfill(16),
             'trace_flags':1,'remote':False},'attributes':{'count':1},
             'dropped_attributes':3}],
  'dropped_attributes':2,'dropped_events':3,'dropped_links':4}})
 channel.request('flush')
assert channel.seal({'function_completions':4,'active_functions':0,'runtime_losses':{},
 'span_observation':{'sampled_started':4,'sampled_ended':4,'handed_off':4}})
print(json.dumps({'result':42,'random_unchanged':before==random.getstate()}))
'''


class PythonSdkProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.policy = Policy.parse({'schema_version': 3,
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 100000, 'timeout_ms': 1000},
            'metrics': {'collection': False, 'emission': False},
            'traces': {'enabled': True, 'sample_ratio': 1.0}})

    def start(self, policy: Policy | None = None, *, bind: bool = True) -> SdkProcess:
        sdk = SdkProcess.start(policy or self.policy, self.root / 'runtime', bind=bind)
        self.addCleanup(sdk.abort)
        return sdk

    def producer(self, **changes):
        return {'function_completions': 0, 'active_functions': 0,
                'runtime_losses': {}, 'span_observation': {
                    'sampled_started': 0, 'sampled_ended': 0, 'handed_off': 0}, **changes}

    def invoke(self, sdk: SdkProcess, code: str) -> subprocess.Popen[str]:
        command = [sys.executable, '-c', code, *(str(fd) for fd in sdk.channel.descriptors),
                   str(sdk.process.pid), sdk.channel.launch_id, sdk.channel.policy_sha256]
        app = subprocess.Popen(command, pass_fds=sdk.channel.descriptors,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        sdk.channel.close()  # Only the application now owns these pipe ends.
        return app

    def test_native_fixture_four_states_rng_output_and_decoded_span_metadata(self) -> None:
        sdk = self.start(bind=False)
        app = self.invoke(sdk, APPLICATION)
        try:
            stdout, stderr = app.communicate(timeout=20)
        finally:
            if app.poll() is None:
                app.kill()
                app.wait(timeout=3)
        self.assertEqual((app.returncode, stderr), (0, ''))
        self.assertEqual(json.loads(stdout), {'result': 42, 'random_unchanged': True})
        report = sdk.wait_report(application_pid=app.pid)
        self.assertEqual((report['phase'], report['sdk_phase'], sdk.process.returncode),
                         ('closed', 'closed', 0))
        self.assertNotEqual(report['sdk_pid'], app.pid)
        self.assertEqual(report['producer']['function_completions'], 4)
        self.assertEqual(report['span_accounting']['sdk_exported'], 4)
        self.assertEqual(report['span_accounting']['queue_dropped'], 0)
        self.assertEqual(self.capture.calls(), 2)
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans), 4)
        for index, span in enumerate(sorted(spans, key=lambda value: value.trace_id)):
            self.assertEqual(span.trace_id.hex(), f'{index + 1:032x}')
            self.assertEqual(span.span_id.hex(), f'{index + 1:016x}')
            self.assertEqual(span.events[0].name, 'event')
            self.assertEqual(span.events[0].dropped_attributes_count, 2)
            self.assertEqual(span.links[0].dropped_attributes_count, 3)
            self.assertEqual((span.dropped_attributes_count, span.dropped_events_count,
                              span.dropped_links_count), (2, 3, 4))
        pids = {attribute.value.int_value for request in self.capture.traces
                for resource in request.resource_spans for attribute in resource.resource.attributes
                if attribute.key == 'process.pid'}
        self.assertEqual(pids, {app.pid})
        self.assertEqual((self.root / 'runtime/python-sdk.log').read_text(), '')

    def test_app_exits_before_delayed_sdk_ack_then_supervisor_observes_real_drain(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['metrics'].update(collection=True, emission=True)
        data['export'].update(timeout_ms=5000, shutdown_timeout_ms=10000)
        sdk = self.start(Policy.parse(data), bind=False)
        self.capture.metric_response_gate.clear()
        code = '''import os,sys
from otelc.python_sdk_process import SdkChannel
channel=SdkChannel(int(sys.argv[1]),int(sys.argv[2]),sdk_pid=int(sys.argv[3]),
 launch_id=sys.argv[4],policy_sha256=sys.argv[5]);channel.bind(os.getpid())
assert channel.writer.send({'kind':'metric','scope':'fixture','instrument':'counter',
 'name':'otelc.function.calls','value':1})
print(42,flush=True)
assert channel.seal({'function_completions':1,'active_functions':0,'runtime_losses':{},
 'span_observation':{'sampled_started':0,'sampled_ended':0,'handed_off':0}})
'''
        app = self.invoke(sdk, code)
        try:
            stdout, stderr = app.communicate(timeout=10)
            self.assertEqual((app.returncode, stdout, stderr), (0, '42\n', ''))
            self.assertTrue(self.capture.metric_request_seen.wait(2))
            self.assertIsNone(sdk.process.poll(), 'body receipt is not SDK acknowledgement')
        finally:
            self.capture.metric_response_gate.set()
            if app.poll() is None:
                app.kill()
                app.wait(timeout=3)
        report = sdk.wait_report(application_pid=app.pid)
        self.assertEqual(report['phase'], 'closed')
        self.assertTrue(report['shutdown_completed'])
        self.assertEqual(report['in_flight_exports'], 0)
        self.assertEqual(self.capture.calls(), 1)

    def test_missing_checkpoint_or_invalid_record_cannot_be_a_clean_run(self) -> None:
        sdk = self.start()
        report = sdk.finish()
        self.assertEqual((report['phase'], report['sdk_phase']), ('failed', 'closed'))
        self.assertFalse(report['producer_checkpoint_available'])
        self.assertIsNone(report['producer'])
        self.assertEqual(sdk.process.returncode, 1)
        sdk = SdkProcess.start(self.policy, self.root / 'invalid')
        self.addCleanup(sdk.abort)
        self.assertTrue(sdk.channel.writer.send({'kind': 'unknown'}))
        report = sdk.finish()
        self.assertEqual(report['phase'], 'failed')
        self.assertEqual(report['protocol_errors'], 1)
        self.assertEqual(sdk.process.returncode, 1)

    def test_incomplete_calls_and_open_lifetimes_are_not_fabricated_as_complete(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['lifetimes'].update(enabled=True, include=['*'])
        sdk = self.start(Policy.parse(data))
        lifetimes = {'admitted': 3, 'gc_completed': 1, 'resource_completed': 0,
            'current_live': 2, 'open_resources': 2, 'tracked_instances': 2,
            'tracked_constructors': 1, 'failed_initialisations': 0,
            'failed_releases': 0, 'censored': 0, 'lost': 0, 'losses': {}}
        report = sdk.finish(self.producer(active_functions=1, lifetimes=lifetimes))
        self.assertEqual(report['phase'], 'failed')
        self.assertEqual(report['producer']['function_completions'], 0)
        self.assertEqual(report['producer']['runtime_losses']['incomplete'], 1)
        observed = report['producer']['lifetimes']
        self.assertEqual((observed['censored'], observed['resource_completed']), (2, 0))
        self.assertEqual((observed['tracked_instances'], observed['tracked_constructors']), (0, 0))

    def test_unacknowledged_export_keeps_live_status_and_bounded_failed_shutdown(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['metrics'].update(collection=True, emission=True)
        data['export'].update(timeout_ms=5000, shutdown_timeout_ms=100)
        sdk = self.start(Policy.parse(data))
        self.capture.metric_response_gate.clear()
        self.assertTrue(sdk.channel.writer.send({'kind': 'metric', 'scope': 'fixture',
            'instrument': 'counter', 'name': 'otelc.function.calls', 'value': 1}))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(sdk.channel.request, 'flush')
            self.assertTrue(self.capture.metric_request_seen.wait(2))
            with self.assertRaises(RuntimeError):
                pending.result(timeout=3)
        state = sdk.channel.controls(collection=False, emission=False)
        self.assertEqual(state['in_flight_exports'], 1)
        self.assertFalse(state['effective_emission_off'])
        self.assertTrue(state['operation_pending'])
        started = time.monotonic()
        report = sdk.finish(self.producer(function_completions=1))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(report['phase'], 'failed')
        self.assertFalse(report['shutdown_completed'])
        self.assertEqual(report['in_flight_exports'], 1)
        self.assertEqual(report['span_accounting']['queue_evidence'], 'unavailable')
        self.assertIsNone(report['span_accounting']['queue_pending'])
        self.capture.metric_response_gate.set()

    def test_concurrent_control_requests_acknowledge_their_own_serial_changes(self) -> None:
        sdk = self.start()
        states = [(False, False), (True, False), (False, True), (True, True)] * 3
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            pending = [pool.submit(sdk.channel.controls, collection=collection, emission=emission)
                       for collection, emission in states]
            results = [future.result(timeout=5) for future in pending]
        self.assertEqual([(state['collection'], state['emission']) for state in results], states)
        self.assertEqual(sdk.finish(self.producer())['phase'], 'closed')

    def test_actual_sdk_queue_drop_is_visible_even_while_metrics_are_off(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['export'].update(timeout_ms=5000, shutdown_timeout_ms=10000)
        sdk = self.start(Policy.parse(data))
        self.capture.trace_response_gate.clear()
        count = 5600

        def send(index):
            value = span(context=context(trace_id=f'{index + 1:032x}',
                                         span_id=f'{index + 1:016x}'))
            self.assertTrue(sdk.channel.writer.send({'kind': 'span', 'span': value}))

        try:
            for index in range(600):
                send(index)
            self.assertTrue(self.capture.trace_request_seen.wait(2))
            for index in range(600, count):
                send(index)
        finally:
            self.capture.trace_response_gate.set()
        before_close = sdk.channel.request('flush')
        self.assertEqual(before_close['span_accounting']['queue_evidence'], 'unavailable')
        report = sdk.finish(self.producer(span_observation={
            'sampled_started': count, 'sampled_ended': count, 'handed_off': count}))
        actual = sum(len(scope.spans) for request in self.capture.traces
                     for resource in request.resource_spans for scope in resource.scope_spans)
        self.assertEqual((report['phase'], report['sdk_phase']), ('failed', 'closed'))
        self.assertEqual(report['traces']['failed'], 0)
        self.assertEqual(report['span_accounting']['received'], count)
        self.assertEqual(report['span_accounting']['sdk_exported'], actual)
        self.assertEqual(report['span_accounting']['queue_dropped'], count - actual)
        self.assertGreater(report['span_accounting']['queue_dropped'], 0)

    def test_disabled_language_existing_directory_and_partial_pipe_failure_preserve_state(self) -> None:
        with self.assertRaises(ValueError):
            SdkProcess.start(Policy.parse({'schema_version': 3, 'languages': ['java']}), self.root / 'disabled')
        self.assertFalse((self.root / 'disabled').exists())
        existing = self.root / 'existing'
        existing.mkdir()
        (existing / 'keep').write_text('original')
        with self.assertRaises(FileExistsError):
            SdkProcess.start(self.policy, existing)
        self.assertEqual((existing / 'keep').read_text(), 'original')
        real_pipe = os.pipe
        allocated = []

        def fail_second_pipe():
            if allocated:
                raise OSError('controlled pipe allocation failure')
            pair = real_pipe()
            allocated.extend(pair)
            return pair

        with mock.patch('otelc.python_sdk_process.os.pipe', side_effect=fail_second_pipe):
            with self.assertRaisesRegex(OSError, 'controlled pipe'):
                SdkProcess.start(self.policy, self.root / 'partial')
        for descriptor in allocated:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_startup_failure_reaps_its_actual_worker_and_closes_descriptors(self) -> None:
        processes = []
        real_spawn = subprocess.Popen

        def spawn(*args, **kwargs):
            process = real_spawn(*args, **kwargs)
            processes.append(process)
            return process

        with mock.patch('otelc.python_sdk_process.subprocess.Popen', side_effect=spawn), \
                mock.patch.object(SdkProcess, '_ready', side_effect=ValueError('controlled readiness failure')):
            with self.assertRaisesRegex(ValueError, 'controlled readiness'):
                self.start()
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_final_report_identity_is_checked_after_actual_worker_exit(self) -> None:
        sdk = self.start()
        report = sdk.finish(self.producer())
        self.assertEqual(report['phase'], 'closed')
        path = self.root / 'runtime/python-sdk-status.json'
        report['launch_id'] = '0' * 32
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            sdk.wait_report()
        report['launch_id'] = sdk.channel.launch_id
        report['phase'] = 'waiting_application'
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'invalid Python SDK final report'):
            sdk.wait_report()
        report['phase'] = 'closed'
        report['pid'] = True
        path.write_text(json.dumps(report))
        with self.assertRaises(ValueError):
            sdk.wait_report(application_pid=1)
