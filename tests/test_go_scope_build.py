"""Versioned private helpers preserve legal Go scopes through public launches."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ci import build_go_upstream as tooling
from otelc import control, go_build, go_prepared_tools, prepared_go
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]
ERROR_TYPE = 'type failure struct{};func(*failure)Error()string{panic("formatter must not run")};'
SLICE_TYPE = 'type failure []uint64;func(failure)Error()string{panic("formatter must not run")};'
CASES = [(name, declaration + ';func Workload()uint64{return 7}', 'Workload()', 0)
         for name, declaration in (('shadow_nil', 'var nil=0'), ('shadow_any', 'var any=0'),
            ('shadow_int', 'var int=0'), ('shadow_bool', 'var bool=0'), ('shadow_panic', 'var panic=0'),
            ('shadow_false', 'var false=true'), ('shadow_true', 'var true=false'))] + [
    ('named_nil', ERROR_TYPE + 'func Workload()(nil error){return &failure{}}', 'Workload()', 2),
    ('noncomparable_nil', SLICE_TYPE + 'func Workload()(nil error){return failure{1}}', 'Workload()', 2),
    ('typed_pointer_nil', ERROR_TYPE + 'func Workload()*failure{return nil}', 'Workload()', 0),
    ('typed_interface_nil', ERROR_TYPE + 'func Workload()error{var value *failure;return value}', 'Workload()', 2),
    ('nil_slice', SLICE_TYPE + 'func Workload()failure{return nil}', 'Workload()', 0),
    ('value_slice', SLICE_TYPE + 'func Workload()failure{return failure{1}}', 'Workload()', 2),
    ('concrete_true', 'var true=false;type failure uint64;func(failure)Error()string{panic("formatter must not run")};'
                      'func Workload()failure{return 1}', 'Workload()', 2),
    ('parameter_nil', ERROR_TYPE + 'func Workload(nil uint64)(err error){return &failure{}}', 'Workload(7)', 2),
    ('parameter_false', 'func Workload(false uint64)uint64{return 7}', 'Workload(7)', 0),
    ('early_init', 'type starter struct{};func(starter)Call()uint64{return Workload()};'
                   'var Ready=interface{Call()uint64}(starter{}).Call();func Workload()uint64{return 7}', 'Workload()', 0)]


class GoScopeFormatTests(unittest.TestCase):
    def test_public_first_build_installs_then_binds_the_explicit_toolchain(self) -> None:
        from otelc.__main__ import main
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        tools, binding = {'go': '/recorded/go'}, object()
        result = {'plain': {}, 'instrumented': {},
                  'selection': {'functions': [], 'types': [], 'allocations': [], 'releases': []}}
        args = ['quux-otelc', 'go-build', '--scope-safe', '--tooling-directory', '/operator/sdk',
                '--go', '/selected/go', '--root', str(root), '--output', '/external/build']
        with patch('sys.argv', args), contextlib.redirect_stdout(io.StringIO()), \
                patch('otelc.config.Policy.load'), \
                patch.object(tooling, 'build', return_value=tools) as install, \
                patch.object(go_prepared_tools, 'prepare', return_value=binding) as prepare, \
                patch.object(go_build, 'build_project', return_value=result) as build:
            self.assertEqual(main(), 0)
        install.assert_called_once_with('/selected/go', Path('/operator/sdk'), scope_safe=True)
        prepare.assert_called_once_with('/recorded/go', Path('/operator/sdk'))
        self.assertIs(build.call_args.kwargs['prepared_tools'], binding)
        self.assertIs(build.call_args.kwargs['scope_safe'], True)

    def test_new_formats_are_explicit_and_discovery_needs_neither_config_nor_compiler(self) -> None:
        for tags, schema in ((False, 6), (True, 7)):
            expected = prepared_go.protocols(build_tags=tags, prepared_tools=True) | {
                'build': schema, 'inspection': 2 if tags else 1,
                'prepared_tools': 2, 'helper_profile': go_build.SCOPE_HELPER_PROFILE}
            self.assertEqual(prepared_go.protocols(build_tags=tags, prepared_tools=True, scope_safe=True), expected)
            args = [str(ROOT / 'bin/quux-otelc'), '--config', '/no-config',
                    'go-protocols', '--scope-safe', '--prepared-tools']
            if tags:
                args.append('--build-tags')
            result = subprocess.run(args, env=dict(os.environ, PATH='/no-tools', OTELC_PYTHON=sys.executable),
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(prepared_go.protocols()['build'], 4)
        self.assertEqual(prepared_go.protocols(build_tags=True)['build'], 5)

    def test_unbound_or_wrong_tooling_profile_is_refused_before_compiler_or_output(self) -> None:
        with patch.object(subprocess, 'run', side_effect=AssertionError('no compiler')):
            for tools, scope in (({}, True), ({'schema_version': 1}, True),
                                  ({'schema_version': 2}, True), ({}, 1)):
                with self.subTest(tools=tools, scope=scope), self.assertRaises(ValueError):
                    go_build.build_project(Path('/no-root'), '.', None, Path('/no-output'), tools, scope_safe=scope)

    def test_predicates_have_no_application_scope_builtin_constants_or_nil(self) -> None:
        for expression, expected in (('true', 'testTrue'), ('false', 'testFalse'),
                                     ('nil != nil', 'testNotNil(nil)'), ('result != nil', 'testNotNil(result)')):
            self.assertEqual(go_build.failure_expression(expression, 'test'), expected)
        for expression in ('', 'nil', ' != nil', 'result == nil', 'result != nil '):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                go_build.failure_expression(expression, 'test')
        with self.assertRaisesRegex(ValueError, 'private bootstrap'):
            go_build.helper('main', 'test', '0' * 64, scope_safe=True)


class ActualGoScopeBuildTests(unittest.TestCase):
    def line(self, process: subprocess.Popen) -> bytes:
        self.assertTrue(select.select([process.stdout], [], [], 15)[0], 'application response timed out')
        raw = process.stdout.readline()
        self.assertTrue(raw, 'application ended before its expected response')
        return raw

    def stop(self, process: subprocess.Popen) -> None:
        from otelc.prepared_python import stop_group
        stop_group(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def test_original_scopes_error_results_init_and_release_survive_build6_and7(self) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='go-scope-app-', dir=ROOT / 'build') as temporary:
            base = Path(temporary).resolve()
            source, installed = base / 'application', base / 'installed'
            source.mkdir()
            (source / 'go.mod').write_text('module example.test/scopes\n\ngo 1.26.0\n')
            imports, calls, statuses = [], [], {}
            for index, (name, body, invocation, status) in enumerate(CASES):
                package = source / name
                package.mkdir()
                if name == 'shadow_bool':
                    body = ('var bool=0;type WorkItem struct{Value,Padding uint64};'
                            'func(item *WorkItem)Close()error{return nil};'
                            'func Workload()uint64{item:=&WorkItem{Value:7};defer item.Close();return item.Value}')
                (package / 'main.go').write_text('package example\n' + body + '\n')
                imports.append(f' c{index} "example.test/scopes/{name}"')
                calls.append(f'_ = c{index}.{invocation}')
                statuses['example.test/scopes/' + name + '.Workload'] = status
            (source / 'main.go').write_text('package main\nimport("bufio";"fmt";"os";\n'
                + '\n'.join(imports) + ')\nfunc main(){fmt.Println("ready",flavor);'
                'scanner:=bufio.NewScanner(os.Stdin);for scanner.Scan(){if scanner.Text()=="quit"{return};'
                + ';'.join(calls) + ';fmt.Println("ok",flavor)}}\n')
            for tag, value in (('!feature', 7), ('feature', 42)):
                (source / ('flavor_' + str(value) + '.go')).write_text(
                    '//go:build ' + tag + '\n\npackage main\nconst flavor=' + str(value) + '\n')
            original = go_build.inputs(source)
            tools = tooling.build(os.environ.get('OTELC_GO', 'go'), installed, scope_safe=True)
            bound = go_prepared_tools.prepare(tools['go'], installed)
            for schema, tags, flavor in ((6, [], 7), (7, ['--build-tag', 'feature'], 42)):
                with self.subTest(schema=schema):
                    capture = Capture()
                    self.addCleanup(capture.close)
                    policy_file = base / ('policy-' + str(schema) + '.toml')
                    policy_file.write_text('schema_version=3\nlanguages=["go"]\n[sources]\ninclude=["**/*.go"]\n'
                        '[functions]\ninclude=["*Workload"]\n[lifetimes]\nenabled=true\ninclude=["*WorkItem"]\n'
                        'release_methods=["Close"]\n[metrics]\ncollection=false\nemission=false\n'
                        '[traces]\nenabled=true\nsample_ratio=1.0\n'
                        f'[export]\nendpoint="http://127.0.0.1:{capture.server.server_port}"\ninterval_ms=100000\n')
                    policy = Policy.load(policy_file)
                    output = base / ('build-' + str(schema))
                    result = subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', str(policy_file),
                        'go-build', '--scope-safe', '--prepared-tools', '--tooling-directory', str(installed),
                        '--go', tools['go'], '--root', str(source), '--output', str(output), *tags],
                        env=dict(os.environ, OTELC_PYTHON=sys.executable), capture_output=True, timeout=600)
                    self.assertEqual(result.returncode, 0, result.stderr[-8192:])
                    receipt = prepared_go.verify_build(output, policy)['receipt']
                    self.assertEqual(receipt['schema_version'], schema)
                    self.assertEqual(receipt['helper_profile'], go_build.SCOPE_HELPER_PROFILE)
                    self.assertEqual({row['name'] for row in receipt['selection']['functions']}, set(statuses))
                    self.assertEqual(len(receipt['selection']['allocations']), 1)
                    self.assertEqual(len(receipt['selection']['releases']), 1)
                    record = output / 'build.json'
                    recorded = record.read_bytes()
                    try:
                        changes = [dict(receipt, helper_profile='unknown'),
                                   dict(receipt, command_evidence={})]
                        for field, wrong in (('schema_version', 1), ('schema_version', True),
                                             ('patch_profile', 'unknown'), ('version_output', 'official')):
                            changed = copy.deepcopy(receipt)
                            changed['toolchain'][field] = wrong
                            changes.append(changed)
                        old = copy.deepcopy(receipt)
                        old['schema_version'] = schema - 2
                        old.pop('helper_profile')
                        if schema == 6:
                            old.pop('command_evidence')
                        changes.append(old)
                        for changed in changes:
                            record.write_text(json.dumps(changed))
                            with patch.object(subprocess, 'run', side_effect=AssertionError('no compiler')), \
                                    self.assertRaises(ValueError):
                                prepared_go.verify_build(output, policy)
                    finally:
                        record.write_bytes(recorded)
                    baseline = subprocess.run([receipt['plain']['path']], cwd=source,
                        input=b'run\n' * 4 + b'quit\n', capture_output=True, timeout=15)
                    self.assertEqual((baseline.returncode, baseline.stderr), (0, b''))
                    self.assertEqual(baseline.stdout, f'ready {flavor}\n'.encode() + f'ok {flavor}\n'.encode() * 4)
                    runtime = base / ('runtime-' + str(schema))
                    prepared_go.prepare(policy, runtime, build_directory=output)
                    process = subprocess.Popen(prepared_go.command(runtime),
                        env=dict(os.environ, PATH='/no-tools'), start_new_session=True,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                    self.addCleanup(self.stop, process)
                    self.assertEqual(self.line(process), f'ready {flavor}\n'.encode())
                    endpoint = runtime / 'control.json'
                    started = runtime / 'go-application-start.json'
                    deadline = time.monotonic() + 3
                    while not started.exists() and time.monotonic() < deadline:
                        time.sleep(.005)
                    pid = prepared_go.read(started)['pid']
                    state = control.wait_ready(endpoint, pid)
                    pid = state['pid']
                    self.assertEqual((state['collection'], state['emission']), (False, False))
                    for collection, emission in ((False, False), (False, True), (True, False), (True, True)):
                        control.request(endpoint, 'collection ' + ('on' if collection else 'off'))
                        state = control.request(endpoint, 'emission ' + ('on' if emission else 'off'))
                        self.assertEqual(state['pid'], pid)
                        before = len(capture.metrics)
                        process.stdin.write(b'run\n'); process.stdin.flush()
                        self.assertEqual(self.line(process), f'ok {flavor}\n'.encode())
                        control.request(endpoint, 'flush')
                        self.assertEqual(len(capture.metrics), before + int(collection and emission))
                    process.stdin.write(b'quit\n'); process.stdin.flush()
                    self.assertEqual(process.wait(timeout=15), 0)
                    self.assertEqual(process.stdout.read(), b'')
                    self.assertEqual(process.stderr.read(), b'')
                    final = prepared_go.execution(runtime, supervisor_pid=process.pid,
                        parent_pid=os.getpid(), returncode=process.returncode)
                    self.assertEqual(final['failures'], {})
                    self.assertEqual(final['application_pid'], pid)
                    self.assertTrue(final['inputs_rechecked'])
                    spans = [span for packet in capture.traces for resource in packet.resource_spans
                             for scope in resource.scope_spans for span in scope.spans]
                    functions = [span for span in spans if span.name in statuses]
                    self.assertEqual(len(functions), 4 * len(statuses) + 1)
                    for name, expected in statuses.items():
                        selected = [span for span in functions if span.name == name]
                        self.assertEqual(len(selected), 5 if '/early_init.' in name else 4)
                        self.assertEqual([span.status.code for span in selected], [expected] * len(selected))
                    lifetimes = [span for span in spans if span.name.endswith('WorkItem lifetime')]
                    # GC observation and successful Close are separate boundaries.
                    # A full run can collect objects between calls; a short run need not.
                    boundaries = [(span, next((item.value.string_value for item in span.attributes
                                   if item.key == 'boundary'), None)) for span in lifetimes]
                    self.assertTrue(all(boundary in ('resource', 'observed_gc') for _, boundary in boundaries))
                    self.assertEqual(len({span.span_id for span in lifetimes}), len(lifetimes))
                    releases = [span for span, boundary in boundaries if boundary == 'resource']
                    self.assertEqual(len(releases), 4)
                    self.assertLessEqual(sum(boundary == 'observed_gc' for _, boundary in boundaries), 4)
                    latest = {}
                    for packet in capture.metrics:
                        for resource in packet.resource_metrics:
                            for scope in resource.scope_metrics:
                                for metric in scope.metrics:
                                    if metric.name in ('otelc.function.calls', 'otelc.function.errors'):
                                        latest[metric.name] = sum(point.as_int for point in metric.sum.data_points)
                    self.assertEqual(latest['otelc.function.calls'], 2 * len(statuses))
                    self.assertEqual(latest['otelc.function.errors'], 2 * sum(status == 2 for status in statuses.values()))
                    self.assertEqual(go_build.inputs(source), original)
                    bound.verify(tools)
