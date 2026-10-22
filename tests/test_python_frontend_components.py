"""Direct component calls complement VM callbacks which suppress trace coverage."""

from __future__ import annotations

import json
import contextlib
import io
import os
import random
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from opentelemetry import trace

from otelc import control, python as adapter
from otelc.config import Policy
from otelc.python_control import ControlServer
from otelc.python_monitor import PythonMonitor
from otelc.python_runtime import PythonRuntime
from otelc.python_sdk_process import SdkProcess
from otelc.python_startup import initialise
from otelc.python_tracing import OsIdGenerator, encoded_context
from test_java_integration import Capture


class PythonFrontendComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.policy = Policy.parse({'schema_version': 3,
            'sources': {'include': ['*.py']}, 'functions': {'include': ['*']},
            'metrics': {'collection': False, 'emission': False},
            'traces': {'enabled': True, 'sample_ratio': 1.0},
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 100000, 'timeout_ms': 1000}})
        self.sdk = SdkProcess.start(self.policy, self.root / 'sdk')
        self.addCleanup(self.sdk.abort)
        self.runtime = PythonRuntime(self.policy, self.sdk.channel)

    def finish(self):
        self.runtime.close()
        return self.sdk.wait_report()

    def test_direct_source_observations_four_states_and_transition_admission(self) -> None:
        before = random.getstate()
        self.runtime.enter(1, 'fixture.workload')
        self.runtime.controls(True, True)
        self.runtime.leave(1, None)  # Not admitted when entry was off.
        self.runtime.enter(2, 'fixture.workload')
        self.runtime.controls(False, True)
        self.runtime.leave(2, None)  # Disabled before completion.
        self.runtime.controls(True, False)
        self.runtime.enter(3, 'fixture.workload')
        self.runtime.suspend(3)
        self.runtime.resume(3)
        self.runtime.leave(3, ValueError('literal'))
        self.runtime.controls(True, True)
        self.sdk.channel.request('flush')
        self.assertEqual(self.capture.calls(), 1)
        exemplars = [exemplar for point in self.capture.points('otelc.function.duration')
                     for exemplar in point.exemplars]
        exported_ids = {span.span_id for request in self.capture.traces
                        for resource in request.resource_spans
                        for scope in resource.scope_spans for span in scope.spans}
        self.assertTrue(exemplars)
        self.assertTrue(all(exemplar.span_id in exported_ids for exemplar in exemplars))
        self.runtime.leave(3, None)  # Duplicate completion is not counted.
        self.runtime.suspend(100)
        self.runtime.resume(100)
        self.assertEqual(self.runtime.status()['function_completions'], 3)
        self.assertEqual(random.getstate(), before)
        report = self.finish()
        self.assertEqual(report['phase'], 'closed')
        self.assertEqual(report['span_accounting']['sdk_exported'], 3)
        self.runtime.enter(4, 'after_close')
        self.assertEqual(self.runtime.status()['active_functions'], 0)
        with self.assertRaises(RuntimeError):
            self.runtime.controls(True, True)

    def test_acknowledgement_wait_does_not_hold_the_application_observation_lock(self) -> None:
        original = self.sdk.channel.request
        waiting = threading.Event()
        release = threading.Event()
        result = []

        def delayed(command, **kwargs):
            waiting.set()
            self.assertTrue(release.wait(3))
            return original(command, **kwargs)

        def update():
            result.append(self.runtime.controls(True, True))

        with mock.patch.object(self.sdk.channel, 'request', side_effect=delayed):
            thread = threading.Thread(target=update)
            thread.start()
            try:
                self.assertTrue(waiting.wait(2))
                started = time.monotonic()
                self.runtime.enter(1, 'fixture.workload')
                self.runtime.leave(1, None)
                self.assertLess(time.monotonic() - started, .5)
            finally:
                release.set()
                thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertEqual(self.finish()['phase'], 'closed')
        self.assertEqual(self.capture.calls(), 1)

    def test_real_control_protocol_authentication_invalid_clients_and_serial_switches(self) -> None:
        directory = self.root / 'control'
        directory.mkdir(mode=0o700)
        server = ControlServer(self.runtime, directory)
        self.addCleanup(server.close)
        descriptor = directory / 'control.json'
        endpoint = json.loads(descriptor.read_text())
        for command, expected in (('on', (True, True)), ('emission off', (True, False)),
                ('collection off', (False, False)), ('emission on', (False, True)),
                ('off', (False, False)), ('on', (True, True))):
            response = control.request(descriptor, command)
            self.assertEqual((response['collection'], response['emission']), expected)
        self.runtime.enter(1, 'fixture.workload')
        self.runtime.leave(1, None)
        control.request(descriptor, 'flush')
        self.assertEqual(control.request(descriptor, 'status')['function_completions'], 1)
        with self.assertRaises(ValueError):
            control.request(descriptor, 'health')
        requests = (b'{}\n', b'not-json\n', b'{' + b'x' * 5000 + b'\n',
                    json.dumps({'token': 'wrong', 'command': 'on'}).encode() + b'\n')
        for request in requests:
            with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=1) as client:
                client.sendall(request)
                self.assertEqual(client.recv(1), b'')
        server.close()
        server.close()
        self.assertEqual(self.finish()['phase'], 'closed')

    def test_public_sdk_ids_attributes_sampling_and_failure_accounting(self) -> None:
        ids = OsIdGenerator()
        with mock.patch('otelc.python_tracing.os.urandom', side_effect=[b'\0' * 8, b'\1' * 8]):
            self.assertEqual(ids.generate_span_id(), int.from_bytes(b'\1' * 8, 'big'))
        self.assertGreater(ids.generate_trace_id(), 0)
        self.assertTrue(ids.is_trace_id_random())
        self.assertIsNone(encoded_context(None))
        self.runtime.enter(1, 'fixture.workload')
        active = trace.get_current_span()
        active.set_attribute('array', [1, 2])
        active.add_event('event', {'ok': True})
        self.runtime.leave(1, None)
        self.assertEqual(self.finish()['phase'], 'closed')
        raw = next(span for request in self.capture.traces for resource in request.resource_spans
                   for scope in resource.scope_spans for span in scope.spans)
        values = {attribute.key: attribute.value for attribute in raw.attributes}
        self.assertEqual([v.int_value for v in values['array'].array_value.values], [1, 2])

    def test_sdk_event_and_link_attribute_drops_survive_the_process_bridge(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider, SpanLimits
        from opentelemetry.sdk.resources import Resource
        from otelc.python_tracing import HandoffSpanProcessor
        provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False,
            id_generator=OsIdGenerator(), span_limits=SpanLimits(max_event_attributes=1,
                                                               max_link_attributes=1))
        processor = HandoffSpanProcessor(self.sdk.channel.writer)
        provider.add_span_processor(processor)
        tracer = provider.get_tracer('fixture')
        link = trace.Link(trace.SpanContext(1, 2, False), {'a': 1, 'b': 2, 'c': 3})
        span = tracer.start_span('fixture.workload', links=[link])
        span.add_event('event', {'a': 1, 'b': 2, 'c': 3})
        span.end()
        counts, errors = processor.snapshot()
        self.assertEqual(errors, 0)
        self.sdk.channel.seal({'function_completions': 1, 'active_functions': 0,
            'runtime_losses': {}, 'span_observation': counts})
        report = self.sdk.wait_report()
        provider.shutdown()
        self.assertEqual(report['phase'], 'closed')
        raw = next(span for request in self.capture.traces for resource in request.resource_spans
                   for scope in resource.scope_spans for span in scope.spans)
        self.assertEqual((raw.events[0].dropped_attributes_count,
                          raw.links[0].dropped_attributes_count), (2, 2))

    def test_direct_callbacks_preserve_values_and_report_failure_boundaries(self) -> None:
        # These are internal callback fixtures, not the source-free developer
        # example. The separate VM tests prove actual event dispatch/order.
        monitor = PythonMonitor(self.policy, self.root, self.runtime)
        original_context = trace.get_current_span()
        namespace = {'monitor': monitor}
        source = '''def workload():
    code = workload.__code__
    monitor._start(code, 0)
    monitor._yield(code, 0, None)
    monitor._resume(code, 0)
    monitor._return(code, 0, 42)
    return 42
'''
        exec(compile(source, str(self.root / 'fixture.py'), 'exec'), namespace)
        self.assertEqual(namespace['workload'](), 42)
        self.assertEqual(self.runtime.completed, 1)
        with mock.patch.object(self.runtime, 'enter', side_effect=ValueError('controlled')):
            self.assertEqual(namespace['workload'](), 42)
        with mock.patch.object(self.runtime, 'leave', side_effect=ValueError('controlled')):
            self.assertEqual(namespace['workload'](), 42)
        self.assertIs(trace.get_current_span(), original_context)
        self.assertEqual(len(self.runtime.pending), 0)
        self.assertEqual(self.finish()['phase'], 'failed')
        self.assertEqual(self.runtime.losses['monitor_start'], 1)
        self.assertEqual(self.runtime.losses['monitor_completion'], 1)

    def test_disabled_tracing_and_zero_sampling_do_not_create_sampled_spans_or_consume_rng(self) -> None:
        self.finish()
        for enabled, ratio in ((False, 1.), (True, 0.)):
            data = self.policy.data | {'traces': {'enabled': enabled, 'sample_ratio': ratio}}
            policy = Policy.parse(data)
            sdk = SdkProcess.start(policy, self.root / f'sdk-{enabled}')
            try:
                before = random.getstate()
                runtime = PythonRuntime(policy, sdk.channel)
                runtime.enter(1, 'fixture.workload')
                runtime.leave(1, None)
                runtime.close()
                self.assertEqual(random.getstate(), before)
                report = sdk.wait_report()
                self.assertEqual(report['phase'], 'closed')
                self.assertEqual(report['span_accounting']['sdk_exported'], 0)
            finally:
                sdk.abort()

    def test_monitor_capacity_unavailable_vm_and_control_transport_failures(self) -> None:
        monitor = PythonMonitor(self.policy, self.root, self.runtime)
        namespace = {}
        exec(compile('def workload(): return 42', str(self.root / 'fixture.py'), 'exec'), namespace)
        with mock.patch('otelc.python_monitor.MAX_FUNCTIONS', 0):
            self.assertIsNone(monitor.selected(namespace['workload'].__code__))
        self.assertEqual(self.runtime.losses['function_capacity'], 1)
        with mock.patch('otelc.python_monitor.sys', SimpleNamespace()):
            with self.assertRaisesRegex(ValueError, 'CPython'):
                PythonMonitor(self.policy, self.root, self.runtime)
        with mock.patch.object(self.sdk.channel.writer, 'send', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'handoff'):
                self.runtime.controls(True, True)
        original = self.sdk.channel.request
        with mock.patch.object(self.sdk.channel, 'request', return_value={
                'collection': False, 'emission': False}):
            with self.assertRaisesRegex(ValueError, 'acknowledgement'):
                self.runtime.controls(True, True)
        original('status')
        self.assertEqual(self.finish()['phase'], 'failed')

    def test_candidate_entry_validation_and_actual_supervisor_outcomes(self) -> None:
        entry = self.root / 'entry.py'
        entry.write_text('def workload(): return 42\nassert workload()==42\n')
        self.assertEqual(adapter.invocation(self.root, entry=Path('entry.py'), module=None,
            arguments=('α😀',))[-2:], ['entry.py', 'α😀'])
        self.assertEqual(adapter.invocation(self.root, entry=None, module='αpkg.main',
            arguments=())[-2:], ['-m', 'αpkg.main'])
        for script, module in ((None, None), (entry, 'package'), (None, 'bad..name'),
                               (self.root, None), (Path(sys.executable), None)):
            with self.assertRaises(ValueError):
                adapter.invocation(self.root, entry=script, module=module, arguments=())
        with mock.patch.object(adapter.sys, 'version_info', (3, 11)):
            with self.assertRaisesRegex(ValueError, 'CPython'):
                adapter.invocation(self.root, entry=entry, module=None, arguments=())
        self.assertEqual(adapter.run(self.policy, self.root / 'plain', root=self.root,
                                     entry=entry, plain=True), 0)
        self.assertFalse((self.root / 'plain').exists())
        self.assertEqual(adapter.run(self.policy, self.root / 'native', root=self.root, entry=entry), 0)
        self.assertEqual(json.loads((self.root / 'native/sdk/python-sdk-status.json').read_text())['phase'], 'closed')
        entry.write_text('raise SystemExit(7)\n')
        self.assertEqual(adapter.run(self.policy, self.root / 'exit', root=self.root, entry=entry), 7)
        entry.write_text('import os\nos._exit(9)\n')
        diagnostic = io.StringIO()
        with contextlib.redirect_stderr(diagnostic):
            self.assertEqual(adapter.run(self.policy, self.root / 'abrupt', root=self.root, entry=entry), 9)
        self.assertIn('unverified', diagnostic.getvalue())
        for changes in ({'languages': ['java']}, {'lifetimes': {'enabled': True}},
                        {'context': {'http': True}}):
            with self.assertRaises(ValueError):
                adapter.run(Policy.parse(self.policy.data | changes), self.root / 'refused',
                            root=self.root, entry=entry)
        self.assertFalse((self.root / 'refused').exists())
        self.assertEqual(self.finish()['phase'], 'closed')

    def test_source_capacity_and_incomplete_observations_fail_honestly(self) -> None:
        self.runtime.enter(1, 'fixture.workload')
        self.runtime.enter(1, 'duplicate')
        with mock.patch('otelc.python_runtime.MAX_ACTIVE', 1):
            self.runtime.enter(2, 'overflow')
        self.runtime.suspend(1)
        report = self.finish()
        self.assertEqual(report['phase'], 'failed')
        self.assertEqual(report['producer']['runtime_losses']['active_capacity'], 2)
        self.assertEqual(report['producer']['active_functions'], 1)
        self.assertEqual(report['producer']['function_completions'], 0)

    def test_trickling_control_input_cannot_extend_its_read_deadline(self) -> None:
        directory = self.root / 'control'
        directory.mkdir(mode=0o700)
        server = ControlServer(self.runtime, directory)
        self.addCleanup(server.close)
        endpoint = json.loads((directory / 'control.json').read_text())
        first = threading.Event()

        def trickle():
            with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=1) as client:
                try:
                    for _ in range(50):
                        client.sendall(b'{')
                        first.set()
                        time.sleep(.05)
                except OSError:
                    pass

        thread = threading.Thread(target=trickle)
        thread.start()
        try:
            self.assertTrue(first.wait(1))
            started = time.monotonic()
            state = control.request(directory / 'control.json', 'status')
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(state['pid'], os.getpid())
        finally:
            thread.join(timeout=3)
            server.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.finish()['phase'], 'closed')

    def test_invalid_startup_plans_and_partial_installation_close_owned_resources(self) -> None:
        for index, value in enumerate(({}, {'schema_version': 99}, {'schema_version': True})):
            path = self.root / f'bad-plan-{index}'
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, json.dumps(value).encode())
            os.lseek(descriptor, 0, os.SEEK_SET)
            with self.assertRaises(ValueError):
                initialise(descriptor)
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        sdk = SdkProcess.start(self.policy, self.root / 'failed-sdk', bind=False)
        self.addCleanup(sdk.abort)
        runtime = self.root / 'failed-startup'
        runtime.mkdir(mode=0o700)
        descriptor, _ = adapter.prepare(self.policy, runtime, sdk, self.root)
        plan = json.loads((runtime / 'python-plan.json').read_text())
        inherited = tuple(os.dup(fd) for fd in sdk.channel.descriptors)
        plan['events_fd'], plan['replies_fd'] = inherited
        os.ftruncate(descriptor, 0)
        os.write(descriptor, json.dumps(plan).encode())
        os.lseek(descriptor, 0, os.SEEK_SET)
        sdk.channel.close()
        with mock.patch.object(PythonMonitor, 'install', side_effect=ValueError('controlled')):
            with self.assertRaisesRegex(ValueError, 'controlled'):
                initialise(descriptor)
        for fd in inherited:
            with self.assertRaises(OSError):
                os.fstat(fd)
        report = sdk.wait_report(application_pid=os.getpid())
        self.assertEqual(report['phase'], 'failed')
        self.assertEqual(report['producer']['runtime_losses']['bootstrap'], 1)
        self.assertEqual(self.finish()['phase'], 'closed')

    def test_startup_owns_resources_and_unregisters_its_exit_hook(self) -> None:
        # A real inherited plan/SDK exercises startup directly so its lifecycle
        # is covered even when child-process/VM trace coverage is unavailable.
        sdk = SdkProcess.start(self.policy, self.root / 'startup-sdk', bind=False)
        self.addCleanup(sdk.abort)
        runtime = self.root / 'startup'
        runtime.mkdir(mode=0o700)
        descriptor, _ = adapter.prepare(self.policy, runtime, sdk, self.root)
        # Duplicate descriptor ownership as a native child would inherit it.
        plan = json.loads((runtime / 'python-plan.json').read_text())
        plan['events_fd'], plan['replies_fd'] = (os.dup(fd) for fd in sdk.channel.descriptors)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, json.dumps(plan).encode())
        os.lseek(descriptor, 0, os.SEEK_SET)
        sdk.channel.close()
        application = initialise(descriptor)
        try:
            code = compile('def workload(): return 42\n', str(self.root / 'fixture.py'), 'exec')
            namespace = {}
            exec(code, namespace)
            self.assertEqual(namespace['workload'](), 42)
            self.assertEqual(control.request(runtime / 'control.json', 'status')['pid'], os.getpid())
        finally:
            application.close()
            application.close()
        try:
            self.assertEqual(sdk.wait_report(application_pid=os.getpid())['phase'], 'closed')
        finally:
            application.runtime.channel.close()
        self.assertEqual(self.finish()['phase'], 'closed')
