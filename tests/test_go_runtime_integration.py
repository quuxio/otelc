"""Actual generated Go bootstrap, common policy/controls, health and decoded OTLP."""
from __future__ import annotations

import hashlib
from itertools import product
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from ci import build_go_upstream as tooling
from otelc import control, file_inputs, go_context_bundle as context, go_runtime_config, health
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]
HOOKS = '''package main
import (
 official "go.opentelemetry.io/otelc/pkg/runtime"
 owned "io.quux.otelc/go/runtime"
 "io.quux.otelc/go/probe"
)
var __otelcOwned=func()*owned.Runtime{
 r,err:=owned.FromEnvironment(official.ContextWithImplicitTelemetry,official.DetachSpanFromGLS)
 if err!=nil{panic(err)};return r
}()
var __otelcProbes=__otelcOwned.Probes
func __otelcTrack(item *WorkItem){probe.Track(__otelcProbes,item,0)}
func __otelcRelease(item *WorkItem,success bool){probe.Release(__otelcProbes,item,success)}
'''
RULES = '''finish:
  target: main
  where:
    func: main
  do:
    - inject_code:
        raw: 'defer __otelcOwned.Close()'
function:
  target: main
  where:
    func: workload
  do:
    - inject_code:
        raw: 'defer __otelcProbes.Start(0).End(false)'
factory:
  target: main
  where:
    func: newWorkItem
  do:
    - inject_code:
        raw: |
          __otelcCompletion := __otelcProbes.Watch()
          defer func(){if __otelcCompletion.End(false){__otelcTrack(_unnamedRetVal0)}}()
release:
  target: main
  where:
    func: close
    recv: '*WorkItem'
  do:
    - inject_code:
        raw: |
          __otelcCompletion := __otelcProbes.Watch()
          defer func(){__otelcRelease(item,__otelcCompletion.End(_unnamedRetVal0!=nil))}()
'''


class GoRuntimeIntegrationTests(unittest.TestCase):
    def test_common_config_live_controls_owned_health_and_actual_receipt(self) -> None:
        tools = tooling.build('go', ROOT / 'build/go-upstream')
        environment = tooling.compiler_environment(tools, ROOT / 'build/go-upstream')

        def build(argv: list[str], cwd: Path) -> bytes:
            result = subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, timeout=300)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace')[-5000:])
            return result.stdout

        def exchange(process: subprocess.Popen, message: str) -> bytes:
            process.stdin.write(message.encode() + b'\n')
            process.stdin.flush()
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'application response timed out')
            line = process.stdout.readline()
            self.assertTrue(line, 'application exited before response')
            return line

        with tempfile.TemporaryDirectory(prefix='go-owned-runtime-', dir=ROOT / 'build') as temporary:
            root = Path(temporary).resolve()
            scratch = root / 'compiler-temp'
            scratch.mkdir()
            environment.update(GOTMPDIR=str(scratch), TMPDIR=str(scratch))
            observed = json.loads(build(['go', 'env', '-json', 'GOVERSION', 'GOROOT'], root))
            self.assertEqual(observed, {'GOVERSION': tools['go_version'], 'GOROOT': str(Path(tools['go']).parent.parent)})
            original = root / 'original'
            original.mkdir()
            original_app = original / 'examples/go'
            original_app.mkdir(parents=True)
            source = (ROOT / 'tests/fixtures/go-runtime/App.go').read_bytes()
            module = b'module example.com/otelc-owned-runtime-fixture\n\ngo 1.26.0\n'
            (original_app / 'App.go').write_bytes(source)
            (original / 'go.mod').write_bytes(module)
            private = root / 'private'
            shutil.copytree(original, private)
            private_app = private / 'examples/go'
            sdk = root / 'sdk'
            shutil.copytree(ROOT / 'adapters/go', sdk)
            frozen = {str(p.relative_to(sdk)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in sdk.rglob('*') if p.is_file()}
            modules = root / 'context'
            context.prepare(Path(tools['bundle']), modules, tasks=False)
            rules = private / 'otelc_rules'
            rules.mkdir()
            (rules / 'doc.go').write_text('package rules\n')
            (rules / 'otelc.yaml').write_text(RULES)
            (private_app / 'otelc_probe.go').write_text(HOOKS)
            (private / 'otel.instrumentation.go').write_text('''//go:build tools

package tools
import (
 _ "example.com/otelc-owned-runtime-fixture/otelc_rules"
 _ "go.opentelemetry.io/otelc/instrumentation/runtime"
 _ "go.opentelemetry.io/otelc/instrumentation/go.opentelemetry.io/otel/sdk/trace"
)
''')
            for name, path in {**context.replacements(modules), 'io.quux.otelc/go': sdk}.items():
                build([tools['go'], 'mod', 'edit', '-replace=' + name + '=' + str(path),
                       '-require=' + name + '@v0.0.0'], private)
            build([tools['go'], 'mod', 'edit', '-require=go.opentelemetry.io/otel/sdk@v1.47.0'], private)
            build([tools['go'], 'mod', 'tidy'], private)
            build([tools['go'], 'build', '-o', str(root / 'stock'), './examples/go'], original)
            build([tools['weaver'], '--work-dir', str(root / 'weaver-work'), 'go', 'build',
                   '-o', str(root / 'instrumented'), './examples/go'], private)
            receipts = []
            base = Policy.load(ROOT / 'examples/otelc.toml')
            for index, initial in enumerate(product((False, True), repeat=2)):
                with self.subTest(initial=initial):
                    capture = Capture()
                    self.addCleanup(capture.close)
                    data = json.loads(json.dumps(base.data))
                    data['export'].update(endpoint=f'http://127.0.0.1:{capture.server.server_port}', interval_ms=100000,
                                          timeout_ms=1000, shutdown_timeout_ms=1000)
                    data['metrics'].update(collection=initial[0], emission=initial[1])
                    data['traces'].update(enabled=True, sample_ratio=1.0)
                    policy = Policy.parse(data)
                    self.assertTrue(policy.selection('sources').accepts('examples/go/App.go'))
                    directory = root / f'runtime-{index}'
                    selection = hashlib.sha256(source + RULES.encode()).hexdigest()
                    configuration = go_runtime_config.prepare(policy, directory, functions=('workload',),
                                                             lifetimes=(('WorkItem', True),), selection_sha256=selection)
                    plain = subprocess.Popen([str(root / 'stock')], cwd=original, env=environment,
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                    instrumented = subprocess.Popen([str(root / 'instrumented')], cwd=original,
                        env=environment | configuration['environment'], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                    self.addCleanup(self.stop, plain)
                    self.addCleanup(self.stop, instrumented)
                    endpoint = directory / 'control.json'
                    state = control.wait_ready(endpoint, instrumented.pid)
                    self.assertEqual((state['collection'], state['emission']), initial)
                    stages = []
                    collecting_calls = actions = 0
                    settings = [initial, (True, True), (True, False), (False, True), (False, False), (True, True), (True, True)]
                    for stage, (collection, emission) in enumerate(settings):
                        mode = 1 if stage == 5 else 2 if stage == 6 else 0
                        control.request(endpoint, 'collection ' + ('on' if collection else 'off'))
                        state = control.request(endpoint, 'emission ' + ('on' if emission else 'off'))
                        self.assertEqual(state['pid'], instrumented.pid)
                        before = len(capture.metrics)
                        expected = exchange(plain, str(mode))
                        self.assertEqual(exchange(instrumented, str(mode)), expected)
                        self.assertEqual(json.loads(expected), {'value': 42, 'formatters': 0})
                        self.assertEqual(exchange(instrumented, 'gc'), exchange(plain, 'gc'))
                        actions += 1
                        collecting_calls += int(collection)
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            live = control.request(endpoint, 'health')
                            if int(live['probe_accounting']['gc_observed']) == actions:
                                break
                            time.sleep(.01)
                        self.assertEqual(int(live['probe_accounting']['gc_observed']), actions)
                        control.request(endpoint, 'flush')
                        self.assertEqual(len(capture.metrics), before + int(collection and emission))
                        if collection and emission:
                            self.assertEqual(capture.calls(), collecting_calls)
                        stages.append({'collection': collection, 'emission': emission, 'mode': mode,
                                       'output': expected.decode(), 'health': live})
                    descriptor = json.loads(endpoint.read_bytes())
                    blocked = socket.create_connection(('127.0.0.1', descriptor['port']))
                    blocked.sendall(b'{')
                    started = time.monotonic()
                    exchange_finish = b'quit\n'
                    plain.stdin.write(exchange_finish)
                    plain.stdin.flush()
                    instrumented.stdin.write(exchange_finish)
                    instrumented.stdin.flush()
                    self.assertEqual(plain.wait(timeout=5), 0)
                    self.assertEqual(instrumented.wait(timeout=5), 0)
                    blocked.close()
                    self.assertLess(time.monotonic() - started, 3)
                    self.assertEqual(plain.stderr.read(), b'')
                    self.assertEqual(instrumented.stderr.read(), b'')
                    self.assertEqual(plain.stdout.read(), b'')
                    self.assertEqual(instrumented.stdout.read(), b'')
                    final = health.read(directory / 'health.json')
                    self.assertEqual(final['pid'], instrumented.pid)
                    self.assertEqual(final['phase'], 'final')
                    self.assertEqual(final['lifecycle'], {'flush': 'succeeded', 'shutdown': 'succeeded'})
                    self.assertEqual(final['probe_accounting']['function_started'], str(actions))
                    self.assertEqual(final['probe_accounting']['function_ended'], str(actions))
                    for counter in ('function_active', 'function_lost', 'observer_active', 'observer_lost', 'unwind_unknown', 'lifetime_lost', 'objects_pending'):
                        self.assertEqual(final['probe_accounting'][counter], '0')
                    self.assertEqual(final['probe_accounting']['resource_released'], str(actions - 1))
                    self.assertEqual(final['probe_accounting']['resources_unreleased'], '1')
                    self.assertEqual(final['trace_exports']['queue_pending'], '0')
                    self.assertEqual(final['trace_exports']['queue_dropped'], '0')
                    spans = [span for request in capture.traces for resource in request.resource_spans
                             for scope in resource.scope_spans for span in scope.spans]
                    functions = [span for span in spans if span.name == 'workload']
                    lifetimes = [span for span in spans if span.name == 'WorkItem lifetime']
                    self.assertEqual(len(functions), actions)
                    self.assertEqual(len(lifetimes), actions * 2 - 1)
                    self.assertEqual(len(spans), actions * 3 - 1)
                    parents = {(span.trace_id, span.span_id) for span in functions}
                    for span in lifetimes:
                        self.assertFalse(span.parent_span_id)
                        self.assertEqual(len(span.links), 1)
                        self.assertIn((span.links[0].trace_id, span.links[0].span_id), parents)
                    self.assertEqual(configuration['config'], file_inputs.identity(directory / 'config.json'))
                    receipts.append({'initial_collection': initial[0], 'initial_emission': initial[1],
                                     'plain_pid': plain.pid, 'application_and_sdk_pid': instrumented.pid,
                                     'stages': stages, 'final': final, 'decoded_spans': len(spans),
                                     'metric_requests': len(capture.metrics)})
                    self.stop(plain)
                    self.stop(instrumented)
                    capture.close()
            self.assertEqual((original_app / 'App.go').read_bytes(), source)
            self.assertEqual((private_app / 'App.go').read_bytes(), source)
            self.assertEqual((original / 'go.mod').read_bytes(), module)
            self.assertEqual({str(p.relative_to(sdk)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sdk.rglob('*') if p.is_file()}, frozen)
            (ROOT / 'build/go-runtime-integration.json').write_text(json.dumps({
                'compiler': observed, 'source_sha256': context.digest(source),
                'actual_applications': 8, 'sdk_embedded_application_processes': 4,
                'original_and_private_source_held': True, 'private_sdk_files_held': len(frozen),
                'cases': receipts,
                'scope': 'Compiler-generated owned runtime and common controls/health only; general public source build/launch, HTTP/library and oteleq provider admission remain separate.'}, indent=2) + '\n')

    @staticmethod
    def stop(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
