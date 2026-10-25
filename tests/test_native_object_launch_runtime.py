"""Compiler-produced object probes through the real launcher SDK HTTP pipeline.

The host supplies the new internal plan setting explicitly. Public build/policy
translation is deliberately not claimed by this component test.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time
import unittest

from otelc import control, health, llvm_backend, native_launch, native_lifetimes
from otelc.config import Policy
from test_health import report as health_fixture
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]
COUNTERS = ('types', 'admitted', 'completed', 'live', 'destroying', 'ending',
            'censored', 'lost', 'duplicates', 'invalid')


class ObjectHealthTests(unittest.TestCase):
    def test_object_accounting_is_closed_and_only_valid_for_cpp(self) -> None:
        value = health_fixture()
        value.update(language='cpp', source_complete=False,
                     function_accounting={**dict.fromkeys(('registered', 'started', 'ended',
                         'active', 'lost', 'invalid', 'metadata_errors'), '0'), 'closed': True},
                     object_accounting={**dict.fromkeys(COUNTERS, '0'), 'closing': True, 'closed': True})
        self.assertIs(health.validate(value), value)
        for key, bad in [('admitted', None), ('live', '-1'), ('lost', 0), ('closed', 1), ('extra', '0')]:
            changed = copy.deepcopy(value)
            changed['object_accounting'][key] = bad
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'object accounting'):
                health.validate(changed)
        for language in ('java', 'c'):
            changed = copy.deepcopy(value)
            changed['language'] = language
            with self.subTest(language=language), self.assertRaisesRegex(ValueError, 'object accounting'):
                health.validate(changed)


@unittest.skipUnless(os.environ.get('OTELC_NATIVE_LIBRARY'), 'select the built native launch SDK library')
class ObjectLaunchRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix='native-object-http-', dir=ROOT / 'build')
        cls.base = Path(cls.temporary.name).resolve()
        cls.library = Path(os.environ['OTELC_NATIVE_LIBRARY']).resolve(strict=True)
        cls.root = ROOT / 'tests/fixtures'
        cls.source = cls.root / 'native-objects.cpp'
        cls.original = cls.source.read_bytes()
        policy = Policy.parse({'schema_version': 3, 'languages': ['cpp'],
            'sources': {'include': ['**']}, 'functions': {'include': ['object_fixture_*']},
            'lifetimes': {'enabled': True, 'include': ['native_object_fixture::*Item']}})
        cls.plugin = llvm_backend.build(cls.base / 'plugin', function_registry=True,
                                       source_aware=True, object_lifetimes=True)
        manifest = cls.plugin.manifest()
        inventory = native_lifetimes.inspect(policy, cls.root, [cls.source], cls.base / 'inventory')
        cls.binaries = Path(manifest['toolchain']['directory'])
        environment = llvm_backend.environment(policy, 'cpp', source_root=cls.root)
        environment['OTELC_OBJECT_INVENTORY'] = str(cls.base / 'inventory/native-lifetimes.json')
        assert inventory['source_complete'] is False
        bootstrap = cls.base / 'bootstrap.cpp'
        bootstrap.write_text(native_launch.BOOTSTRAP)
        driver = cls.base / 'driver.cpp'
        driver.write_text('#include <stdio.h>\n#include <thread>\n#include <vector>\n'
            'extern "C" {int object_fixture_run();int object_fixture_failed();'
            'int object_fixture_unwind();int object_fixture_reuse();'
            'int object_fixture_throwing_destructor();void*object_fixture_make();void object_fixture_delete(void*);}\n'
            'int main(){int x;std::vector<void*> held;while(scanf("%d",&x)==1){int r=0;'
            'if(x==1)r=object_fixture_run();else if(x==2){held.push_back(object_fixture_make());r=9;}'
            'else if(x==3&&!held.empty()){void*p=held.back();held.pop_back();'
            'std::thread t([p]{object_fixture_delete(p);});t.join();}'
            'else if(x==4)r=object_fixture_failed();else if(x==5)r=object_fixture_throwing_destructor();'
            'else if(x==6)r=object_fixture_unwind();else if(x==7)r=object_fixture_reuse();'
            'printf("%d\\n",r);fflush(stdout);}return 0;}\n')
        for lane in ('plain', 'instrumented'):
            obj = cls.base / (lane + '.o')
            command = [str(cls.binaries / 'clang++'), '-std=c++17', '-O2', '-g', '-fexceptions',
                       '-c', str(cls.source), '-o', str(obj)]
            if lane == 'instrumented':
                command.append('-fpass-plugin=' + manifest['plugin']['path'])
            native_launch._compile(command, cls.root, cls.base, lane + '-compile', environment)
            command = [str(cls.binaries / 'clang++'), '-std=c++17', '-pthread']
            if lane == 'instrumented':
                command += [str(bootstrap), '-I', str(ROOT / 'adapters/native/launcher/include')]
            command += [str(obj), str(driver), '-o', str(cls.base / lane)]
            if lane == 'instrumented':
                command += [str(cls.library), '-Wl,-rpath,' + str(cls.library.parent)]
            native_launch._compile(command, cls.base, cls.base, lane + '-link', environment)

    @classmethod
    def tearDownClass(cls) -> None:
        assert cls.source.read_bytes() == cls.original
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.folder = self.base / self._testMethodName
        self.folder.mkdir(mode=0o700)
        self.capture = Capture()
        self.addCleanup(self.capture.close)

    def start(self, *, collection=False, emission=False, traces=True, limit=4096) -> subprocess.Popen:
        # An explicit internal plan exercises the SDK without suggesting that
        # compiler contexts have already been connected to public preparation.
        settings = {'service_name': 'native-object-runtime',
            'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
            'interval_ms': 60000, 'timeout_ms': 1000, 'shutdown_timeout_ms': 5000,
            'collection': collection, 'emission': emission, 'traces': traces, 'sample_ratio': 1,
            'boundaries': [1e-6, 1e-5, 1e-4, .001, .01, .1, 1.],
            'object_lifetimes': {'enabled': True, 'max_live': limit}}
        plan = {'schema_version': 1, 'language': 'cpp', 'runtime': str(self.folder),
                'policy_sha256': 'a' * 64, 'selection_sha256': 'b' * 64,
                'token': 'd' * 64, 'settings': settings}
        path = self.folder / 'plan.json'
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.write(descriptor, json.dumps(plan).encode())
            process = subprocess.Popen([str(self.base / 'instrumented')],
                env=dict(os.environ, OTELC_NATIVE_PLAN_FD=str(descriptor)), pass_fds=(descriptor,),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            os.close(descriptor)
        self.addCleanup(self.stop, process)
        deadline = time.monotonic() + 10
        while not (self.folder / 'control.json').exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(.01)
        self.assertTrue((self.folder / 'control.json').exists(), 'object SDK did not attach')
        return process

    @staticmethod
    def stop(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def call(self, process: subprocess.Popen, number: int, expected: int) -> None:
        process.stdin.write(f'{number}\n'.encode()); process.stdin.flush()
        self.assertTrue(select.select([process.stdout], [], [], 10)[0], 'application response timed out')
        self.assertEqual(process.stdout.readline(), f'{expected}\n'.encode())

    def finish(self, process: subprocess.Popen) -> dict:
        output, errors = process.communicate(timeout=15)
        self.assertEqual((process.returncode, output, errors), (0, b'', b''))
        value = health.read(self.folder / 'health.json')
        self.assertEqual(value['lifecycle']['shutdown'], 'succeeded')
        self.assertEqual(value['function_accounting']['metadata_errors'], '0')
        self.assertTrue(value['object_accounting']['closed'])
        return value

    def spans(self) -> list:
        return [span for request in self.capture.traces for resource in request.resource_spans
                for scope in resource.scope_spans for span in scope.spans]

    def test_original_objects_exceptions_cross_thread_and_censoring_reach_otlp(self) -> None:
        ordinary = subprocess.run([str(self.base / 'plain')], input=b'1\n4\n5\n6\n7\n2\n3\n2\n',
                                  capture_output=True, check=True, timeout=10)
        self.assertEqual((ordinary.stdout, ordinary.stderr), (b'8\n7\n13\n7\n3\n9\n0\n9\n', b''))
        process = self.start(collection=True, emission=True)
        for value, expected in ((1, 8), (4, 7), (5, 13), (6, 7), (7, 3), (2, 9), (3, 0), (2, 9)):
            self.call(process, value, expected)
        report = self.finish(process)
        objects = report['object_accounting']
        self.assertEqual({key: objects[key] for key in ('live', 'destroying', 'ending', 'lost', 'duplicates', 'invalid')},
                         dict.fromkeys(('live', 'destroying', 'ending', 'lost', 'duplicates', 'invalid'), '0'))
        self.assertEqual(int(objects['admitted']), int(objects['completed']) + 1)
        self.assertEqual(objects['censored'], '1')
        spans = self.spans()
        lifetimes = [span for span in spans if span.name.endswith(' lifetime')]
        self.assertEqual(len(lifetimes), int(objects['admitted']))
        attributes = lambda span: {item.key: item.value for item in span.attributes}
        self.assertEqual(sum(attributes(span)['boundary'].string_value == 'shutdown_censored' for span in lifetimes), 1)
        self.assertEqual(sum(attributes(span).get('otelc.destructor.exception_exit', None).bool_value
                             for span in lifetimes if 'otelc.destructor.exception_exit' in attributes(span)), 1)
        functions = {span.span_id: span for span in spans if not span.name.endswith(' lifetime')}
        for span in lifetimes:
            self.assertFalse(span.parent_span_id)
            self.assertGreaterEqual(span.end_time_unix_nano, span.start_time_unix_nano)
            self.assertEqual(len(span.links), 1)
            self.assertIn(span.links[0].span_id, functions)
        instruments = {metric.name for request in self.capture.metrics for group in request.resource_metrics
                       for scope in group.scope_metrics for metric in scope.metrics}
        self.assertTrue({'otelc.lifetime.admitted', 'otelc.lifetime.completed', 'otelc.lifetime.incomplete',
                         'otelc.lifetime.duration', 'otelc.lifetime.live'} <= instruments)

    def test_all_four_live_states_keep_object_traces_and_retained_metrics(self) -> None:
        process = self.start()
        pid = process.pid
        for collection, emission in ((False, False), (True, False), (False, True)):
            control.request(self.folder / 'control.json', 'collection ' + ('on' if collection else 'off'))
            control.request(self.folder / 'control.json', 'emission ' + ('on' if emission else 'off'))
            self.call(process, 7, 3)
            control.request(self.folder / 'control.json', 'flush')
            self.assertFalse(self.capture.metrics)
            self.assertEqual(process.pid, pid)
        control.request(self.folder / 'control.json', 'on')
        self.call(process, 7, 3)
        report = self.finish(process)
        self.assertEqual(report['object_accounting']['admitted'], '8')
        self.assertEqual(report['object_accounting']['completed'], '8')
        self.assertEqual(len([span for span in self.spans() if span.name.endswith(' lifetime')]), 8)
        points = [point.as_int for request in self.capture.metrics for group in request.resource_metrics
                  for scope in group.scope_metrics for metric in scope.metrics
                  if metric.name == 'otelc.lifetime.completed' for point in metric.sum.data_points]
        self.assertTrue(points)
        self.assertEqual(max(points), 4)  # Silent collected interval plus final both-on interval.

    def test_live_capacity_loss_is_reported_and_tracing_can_be_disabled(self) -> None:
        process = self.start(collection=True, emission=True, traces=False, limit=1)
        self.call(process, 2, 9); self.call(process, 2, 9)
        report = self.finish(process)
        self.assertEqual(report['object_accounting']['admitted'], '1')
        self.assertEqual(report['object_accounting']['lost'], '1')
        self.assertEqual(report['object_accounting']['censored'], '1')
        self.assertFalse(self.capture.traces)

    def test_invalid_internal_object_plans_refuse_before_application_calls(self) -> None:
        for index, (language, selected) in enumerate([
            ('cpp', {'enabled': True, 'max_live': 0}),
            ('cpp', {'enabled': True, 'max_live': 1048577}),
            ('cpp', {'enabled': 'true', 'max_live': 1}),
            ('cpp', {'enabled': True, 'max_live': 1, 'extra': True}),
            ('c', {'enabled': True, 'max_live': 1}),
        ]):
            folder = self.folder / str(index)
            folder.mkdir(mode=0o700)
            settings = {'service_name': 'invalid-object-plan', 'collection': True, 'emission': True,
                        'boundaries': [1e-6, .001, 1.], 'object_lifetimes': selected}
            plan = {'schema_version': 1, 'language': language, 'runtime': str(folder), 'settings': settings}
            descriptor = os.open(folder / 'plan.json', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            try:
                os.write(descriptor, json.dumps(plan).encode())
                result = subprocess.run([str(self.base / 'instrumented')], input=b'1\n',
                    env=dict(os.environ, OTELC_NATIVE_PLAN_FD=str(descriptor)), pass_fds=(descriptor,),
                    capture_output=True, timeout=10)
            finally:
                os.close(descriptor)
            with self.subTest(language=language, setting=selected):
                self.assertEqual((result.returncode, result.stdout, result.stderr), (78, b'', b''))
                self.assertFalse((folder / 'control.json').exists())
                self.assertFalse((folder / 'health.json').exists())

