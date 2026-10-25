"""Actual unchanged native applications, official SDK OTLP and live controls."""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from otelc import control, health, native_launch
from otelc.config import Policy
from test_java_integration import Capture


@unittest.skipUnless(os.environ.get('OTELC_NATIVE_LIBRARY'), 'select the built native launch SDK library')
class NativeLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='otelc-native-launch-', dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'application'
        self.root.mkdir()
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.library = Path(os.environ['OTELC_NATIVE_LIBRARY'])

    def policy(self, **changes: Any) -> Policy:
        value = {'schema_version': 3, 'languages': ['c', 'cpp'],
                 'sources': {'include': ['**']}, 'functions': {'include': ['workload*']},
                 'export': {'endpoint': 'http://127.0.0.1:' + str(self.capture.server.server_port),
                            'interval_ms': 60000, 'timeout_ms': 200, 'shutdown_timeout_ms': 3000},
                 'traces': {'enabled': True, 'sample_ratio': 1}}
        value.update(changes)
        return Policy.parse(value)

    def build(self, policy: Policy, language: str = 'c', text: str | None = None,
              name: str = 'build') -> native_launch.Build:
        source = self.root / ('main.c' if language == 'c' else 'main.cpp')
        source.write_text(text or '#include <stdio.h>\n'
            '__attribute__((noinline)) int workload(int x){return x+7;}\n'
            'int main(){int x;while(scanf("%d",&x)==1){printf("%d\\n",workload(x));fflush(stdout);}return 0;}\n')
        return native_launch.build(policy, language, self.root, [source], self.base / name, library=self.library)

    def start(self, built: native_launch.Build, name: str = 'runtime', *, plain: bool = False) -> tuple[subprocess.Popen[bytes], Path]:
        runtime = self.base / name
        process = native_launch.start(built, runtime, plain=plain, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.clean_process, process)
        if not plain:
            deadline = time.monotonic() + 5
            while not (runtime / 'control.json').exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(.01)
            self.assertTrue((runtime / 'control.json').exists(), process.communicate(timeout=3) if process.poll() is not None else 'startup timeout')
        return process, runtime

    @staticmethod
    def clean_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def call(self, process: subprocess.Popen[bytes], number: int = 10) -> bytes:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(f'{number}\n'.encode())
        process.stdin.flush()
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'application call timed out')
        return process.stdout.readline()

    def spans(self) -> list[Any]:
        return [span for request in self.capture.traces for resource in request.resource_spans
                for scope in resource.scope_spans for span in scope.spans]

    def stop(self, process: subprocess.Popen[bytes], runtime: Path) -> dict[str, Any]:
        out, err = process.communicate(timeout=8)
        self.assertEqual((process.returncode, out, err), (0, b'', b''))
        report = health.read(runtime / 'health.json')
        self.assertEqual(report['lifecycle'], {'flush': 'succeeded', 'shutdown': 'succeeded'})
        self.assertEqual(report['function_accounting']['active'], '0')
        self.assertEqual(report['function_accounting']['lost'], '0')
        self.assertEqual(report['function_accounting']['invalid'], '0')
        self.assertEqual(report['function_accounting']['metadata_errors'], '0')
        self.assertEqual(report['diagnostics']['dropped'], '0')
        return report

    def test_all_startup_states_enable_live_without_restart_and_keep_traces(self) -> None:
        for collection, emission in ((False, False), (False, True), (True, False), (True, True)):
            name = f'{int(collection)}{int(emission)}'
            built = self.build(self.policy(metrics={'collection': collection, 'emission': emission}), name='build-' + name)
            process, runtime = self.start(built, 'runtime-' + name)
            endpoint = runtime / 'control.json'
            initial = control.request(endpoint, 'status')
            self.assertEqual((initial['collection'], initial['emission']), (collection, emission))
            self.assertEqual(self.call(process), b'17\n')
            control.request(endpoint, 'flush')
            first = control.request(endpoint, 'health')
            self.assertEqual(first['function_accounting']['ended'], '1')
            self.assertEqual(first['span_accounting']['sampled_ended'], '1')
            self.assertEqual(first['metric_exports']['units_started'] == '0', not (collection and emission))
            control.request(endpoint, 'on')
            self.assertEqual(self.call(process, 11), b'18\n')
            control.request(endpoint, 'flush')
            active = control.request(endpoint, 'health')
            self.assertEqual(active['pid'], process.pid)
            self.assertGreater(int(active['metric_exports']['units_succeeded']), 0)
            control.request(endpoint, 'off')
            self.assertEqual(self.call(process, 12), b'19\n')
            control.request(endpoint, 'flush')
            quiet = control.request(endpoint, 'health')
            self.assertEqual(quiet['metric_exports']['batches_started'], active['metric_exports']['batches_started'])
            self.assertEqual(quiet['span_accounting']['sampled_ended'], '3')
            report = self.stop(process, runtime)
            self.assertEqual(report['trace_exports']['units_succeeded'], '3')
        self.assertEqual(len(self.spans()), 12)

    def test_cpp_original_exceptions_constructors_and_post_main_destructors(self) -> None:
        text = ('#include <cstdio>\n#include <stdexcept>\n'
                '__attribute__((noinline)) int workload(int x){if(x<0)throw std::runtime_error("original");return x+7;}\n'
                'struct Item{Item(){std::printf("before %d\\n",workload(1));}'
                '~Item(){std::printf("after %d\\n",workload(2));}};Item item;\n'
                'int main(){try{workload(-1);}catch(const std::runtime_error&e){std::printf("%s\\n",e.what());}return 0;}\n')
        built = self.build(self.policy(), 'cpp', text)
        expected = b'before 8\noriginal\nafter 9\n'
        for plain in (True, False):
            process, runtime = self.start(built, 'plain' if plain else 'runtime', plain=plain)
            out, err = process.communicate(timeout=8)
            self.assertEqual((process.returncode, out, err), (0, expected, b''))
        report = health.read(runtime / 'health.json')
        self.assertEqual(report['function_accounting']['ended'], '3')
        self.assertEqual(report['span_accounting']['sampled_active'], '0')
        self.assertEqual(report['trace_exports']['units_succeeded'], '3')
        self.assertEqual(sum(span.status.code == 2 for span in self.spans()), 1)
        self.assertEqual(self.capture.calls(), 3)
        self.assertTrue(all(span.name == 'main.cpp::workload(int)' for span in self.spans()))

    def test_control_authentication_duplicate_fields_and_open_connection_shutdown(self) -> None:
        built = self.build(self.policy())
        process, runtime = self.start(built)
        endpoint = json.loads((runtime / 'control.json').read_text())
        for payload in ('{"token":"wrong","command":"off"}',
                        '{"token":"' + endpoint['token'] + '","command":"off","command":"on"}'):
            with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=2) as client:
                client.sendall(payload.encode() + b'\n')
                self.assertIn(b'error', client.recv(4096))
        self.assertTrue(control.request(runtime / 'control.json', 'status')['collection'])
        with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=2) as client:
            client.sendall(b'{')
            before = time.monotonic()
            self.stop(process, runtime)
            self.assertLess(time.monotonic() - before, 2)

    def test_retained_build_refuses_source_library_or_manifest_changes(self) -> None:
        built = self.build(self.policy())
        path = self.root / 'main.c'
        original = path.read_text()
        path.write_text(original + '\n')
        with self.assertRaisesRegex(ValueError, 'changed after building'):
            native_launch.verify(built)
        path.write_text(original)
        native_launch.verify(built)
        manifest = built.directory / 'native-build.json'
        manifest.write_text(built.encoded + '\n')
        with self.assertRaisesRegex(ValueError, 'caller-held build'):
            native_launch.verify(built)

    def test_in_flight_http_does_not_extend_the_application_shutdown_budget(self) -> None:
        policy = self.policy(export={'endpoint': 'http://127.0.0.1:' + str(self.capture.server.server_port),
                                    'interval_ms': 20, 'timeout_ms': 2000, 'shutdown_timeout_ms': 100},
                             traces={'enabled': False})
        built = self.build(policy)
        self.capture.metric_response_gate.clear()
        process, runtime = self.start(built)
        self.assertEqual(self.call(process), b'17\n')
        self.assertTrue(self.capture.metric_request_seen.wait(3), 'actual metric HTTP request not observed')
        before = time.monotonic()
        out, err = process.communicate(timeout=3)
        elapsed = time.monotonic() - before
        self.assertEqual((process.returncode, out, err), (0, b'', b''))
        self.assertLess(elapsed, .8)
        report = health.read(runtime / 'health.json')
        # SDK APIs return only bool; a wait can fail just before our deadline.
        # Either outcome must remain unsuccessful and preserve pending evidence.
        self.assertIn(report['lifecycle']['shutdown'], ('failed', 'timed_out'))
        self.assertEqual(report['span_accounting']['queue_evidence'], 'unavailable')
        self.assertGreater(int(report['metric_exports']['batches_pending']), 0)
        self.capture.metric_response_gate.set()

    def test_failed_export_is_reported_and_later_calls_export_after_recovery(self) -> None:
        self.capture.trace_response_code = 503
        built = self.build(self.policy())
        process, runtime = self.start(built)
        endpoint = runtime / 'control.json'
        self.assertEqual(self.call(process), b'17\n')
        control.request(endpoint, 'flush')
        failed = control.request(endpoint, 'health')
        self.assertGreater(int(failed['trace_exports']['units_failed']), 0)
        self.capture.trace_response_code = 200
        self.assertEqual(self.call(process), b'17\n')
        control.request(endpoint, 'flush')
        recovered = control.request(endpoint, 'health')
        self.assertGreater(int(recovered['trace_exports']['units_succeeded']), 0)
        self.assertEqual(recovered['trace_exports']['units_failed'], failed['trace_exports']['units_failed'])
        self.stop(process, runtime)
        log = runtime / 'agent.log'
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn('failed', log.read_text().lower())

    def test_requested_pending_capabilities_are_refused_before_build(self) -> None:
        for changes in ({'lifetimes': {'enabled': True}}, {'context': {'tasks': True}}, {'context': {'http': True}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                native_launch.validate_policy(self.policy(**changes), 'cpp')
        with self.assertRaises(ValueError):
            native_launch.validate_policy(self.policy(), 'rust')

    def test_public_cli_runs_original_source_and_retains_native_health(self) -> None:
        (self.root / 'main.c').write_text('#include <stdio.h>\nint workload(int x){return x+7;}\n'
                                          'int main(){printf("%d\\n",workload(10));}\n')
        policy = self.base / 'policy.toml'
        policy.write_text('schema_version=3\nlanguages=["c"]\n[sources]\ninclude=["**"]\n'
                          '[functions]\ninclude=["workload*"]\n[export]\nendpoint="http://127.0.0.1:'
                          + str(self.capture.server.server_port) + '"\n[traces]\nenabled=true\nsample_ratio=1.0\n')
        environment = dict(os.environ, OTELC_PYTHON=os.sys.executable)
        runtime = self.base / 'cli-runtime'
        command = [str(native_launch.ROOT / 'bin/quux-otelc'), '--config', str(policy), 'native',
                   '--language', 'c', '--root', str(self.root), '--source', 'main.c',
                   '--build', str(self.base / 'cli-build'), '--runtime', str(runtime),
                   '--sdk-library', str(self.library)]
        result = subprocess.run(command, env=environment, capture_output=True, timeout=30)
        self.assertEqual((result.returncode, result.stdout), (0, b'17\n'), result.stderr)
        self.assertEqual(result.stderr, f'Control descriptor: {runtime}/control.json\n'.encode())
        report = health.read(runtime / 'health.json')
        self.assertEqual(report['function_accounting']['ended'], '1')
        self.assertEqual(report['trace_exports']['units_succeeded'], '1')
