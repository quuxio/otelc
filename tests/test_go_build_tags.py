"""Explicit compiler tags bind original selection, paired builds and live launch."""
from __future__ import annotations

import copy
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
from otelc import control, go_build, go_build_tags as tags, go_source, prepared_go as prepared
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class GoTagInputTests(unittest.TestCase):
    def test_canonical_literal_set_preserves_go_unicode_spelling(self) -> None:
        self.assertEqual(tags.canonical(None), ())
        self.assertEqual(tags.canonical([]), ())
        self.assertEqual(tags.canonical(('tools', '\u03c0', 'feature', 'tools')),
                         ('feature', 'tools', '\u03c0'))
        self.assertEqual(tags.arguments(()), [])
        self.assertEqual(tags.arguments(('feature', '\u03c0')), ['-tags=feature,\u03c0'])
        # Semantic Unicode grammar belongs to the pinned Go parser, not Python.
        self.assertEqual(tags.canonical(('\U0001f600',)), ('\U0001f600',))

    def test_invalid_flag_expression_and_wire_bounds_refuse(self) -> None:
        for value in ('tools', {'tools'}, [1], [''], ['-race'], ['a,b'], ['a b'],
                      ['a&&b'], ['a\n'], ['\0'], ['a' * 257], ['\u03c0' * 129],
                      ['\ud800'], ['a'] * 129):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                tags.canonical(value)
        self.assertEqual(tags.canonical(['\u03c0' * 128]), ('\u03c0' * 128,))

    def test_retained_tags_require_nonempty_already_canonical_list(self) -> None:
        self.assertEqual(tags.record(['feature', 'tools']), ('feature', 'tools'))
        for value in (None, [], (), ['z', 'a'], ['a', 'a'], ('a',)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tags.record(value)

    def test_malformed_tags_refuse_before_any_compiler_or_output(self) -> None:
        with patch('subprocess.run', side_effect=AssertionError('no compiler')):
            with self.assertRaises(ValueError):
                go_build.build_project(Path('/missing'), '.', None, Path('/missing-output'), {},
                                       build_tags=('-race',))
            with self.assertRaises(ValueError):
                go_source.inspect_project(Path('/missing'), '.', {}, Path('/missing-output'),
                                          build_tags=('x,y',))

    def test_untagged_descriptor_stays_exact_and_tagged_discovery_is_offline(self) -> None:
        expected = {'schema_version': 1, 'kind': 'otelc.go.protocols', 'profile': 'go-source-v1',
                    'build': 4, 'config': 2, 'request': 1, 'plan': 1, 'prepared': 1, 'command': 1,
                    'execution': 1, 'health': 4, 'embedded_sdk': True, 'source_complete': False}
        self.assertEqual(prepared.protocols(), expected)
        self.assertEqual(prepared.protocols(build_tags=True), expected | {'build': 5, 'inspection': 2})
        env = dict(os.environ, PATH='/nonexistent', OTELC_PYTHON=sys.executable)
        result = subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', '/no-policy',
                                 'go-protocols', '--build-tags'], env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), prepared.protocols(build_tags=True))


class GoTaggedBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='go-tagged-', dir=ROOT / 'build')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.base = Path(cls.temporary.name).resolve()
        cls.root = cls.base / 'original'
        cls.root.mkdir()
        (cls.root / 'go.mod').write_text('module example.com/tagged\n\ngo 1.26.0\n')
        (cls.root / 'main.go').write_text('''package main
import("bufio";"fmt";"os")
func init(){if name:=os.Getenv("APP_TAG_MARKER");name!=""{os.WriteFile(name,[]byte("executed"),0600)}}
func workload()int{return chosen()}
func main(){fmt.Println("ready");s:=bufio.NewScanner(os.Stdin);for s.Scan(){if s.Text()=="quit"{return};fmt.Println(workload())}}
''')
        (cls.root / 'feature.go').write_text('''//go:build feature && \u03c0 && tools

package main
import _ "embed"
//go:embed asset.txt
var asset string
func chosen()int{return len(asset)+35}
''')
        (cls.root / 'default.go').write_text('''//go:build !(feature && \u03c0 && tools)

package main
func chosen()int{return 7}
''')
        (cls.root / 'excluded.go').write_text('//go:build unavailable\n\npackage main\nfunc invalid( {\n')
        (cls.root / 'asset.txt').write_text('payload')
        cls.original = go_build.inputs(cls.root)
        cls.policy = Policy.parse({'schema_version': 3, 'languages': ['go'],
            'sources': {'include': ['**/*.go']}, 'functions': {'include': ['*workload']},
            'traces': {'enabled': True, 'sample_ratio': 1.0}})
        config = cls.base / 'otelc.toml'
        config.write_text('schema_version=3\nlanguages=["go"]\n[sources]\ninclude=["**/*.go"]\n'
                          '[functions]\ninclude=["*workload"]\n[traces]\nenabled=true\nsample_ratio=1.0\n')
        cls.tools = tooling.build('go', ROOT / 'build/go-upstream')
        cls.build = cls.base / 'tagged'
        cls.marker = cls.base / 'application-was-executed'
        env = dict(os.environ, OTELC_PYTHON=sys.executable, APP_TAG_MARKER=str(cls.marker),
                   GOFLAGS='-tags=unavailable -this-is-not-a-go-option')
        result = subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', str(config), 'go-build',
            '--root', str(cls.root), '--output', str(cls.build), '--go', cls.tools['go'],
            '--build-tag', 'tools', '--build-tag', '\u03c0', '--build-tag', 'feature',
            '--build-tag', 'tools'], env=env, capture_output=True, timeout=600)
        if result.returncode:
            raise AssertionError('public tagged build failed: ' + result.stderr.decode(errors='replace'))
        cls.announced = json.loads(result.stdout)
        cls.receipt = json.loads((cls.build / 'build.json').read_text())

    def test_actual_cli_selects_tagged_files_for_both_builds_and_embeds(self) -> None:
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.announced['schema_version'], 3)
        self.assertEqual(self.receipt['schema_version'], 5)
        self.assertEqual(self.receipt['build_tags'], ['feature', 'tools', '\u03c0'])
        inventory = json.loads((self.build / 'inspection/inventory.json').read_text())
        self.assertEqual(inventory['schema_version'], 2)
        self.assertEqual(inventory['build_tags'], self.receipt['build_tags'])
        self.assertIn('feature.go', inventory['files'])
        self.assertNotIn('default.go', inventory['files'])
        self.assertNotIn('excluded.go', inventory['files'])
        self.assertIn('asset.txt', inventory['compiler_inputs'])
        for row in self.receipt['commands'][-2:]:
            self.assertEqual([arg for arg in row['argv'] if arg.startswith('-tags=')],
                             ['-tags=feature,tools,\u03c0'])
        self.assertTrue((self.build / 'private/otel.instrumentation.go').read_text().startswith('//go:build !feature\n'))
        self.assertEqual(prepared.verify_build(self.build, self.policy)['receipt'], self.receipt)
        result = subprocess.run([str(self.build / 'plain')], input=b'call\nquit\n',
                                capture_output=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'ready\n42\n', b''))
        self.assertEqual(go_build.inputs(self.root), self.original)

    def test_single_tools_tag_uses_stock_default_instead_of_private_tools_package(self) -> None:
        output = self.base / 'tools-only'
        receipt = go_build.build_project(self.root, '.', self.policy, output, self.tools,
                                         build_tags=('tools',))
        self.assertEqual(receipt['build_tags'], ['tools'])
        self.assertTrue((output / 'private/otel.instrumentation.go').read_text().startswith('//go:build !tools\n'))
        self.assertEqual(prepared.verify_build(output, self.policy)['receipt'], receipt)
        for plain in (True, False):
            runtime = self.base / ('single-tools-' + str(plain))
            data = copy.deepcopy(self.policy.data)
            data['metrics'].update(collection=False, emission=False)
            data['traces']['enabled'] = False
            prepared.prepare(Policy.parse(data), runtime, build_directory=output, plain=plain)
            result = subprocess.run(prepared.command(runtime), input=b'call\nquit\n',
                                    capture_output=True, timeout=15, start_new_session=True)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'ready\n7\n', b''))
        self.assertEqual(go_build.inputs(self.root), self.original)

    def test_altered_tag_inventory_command_and_log_evidence_refuse(self) -> None:
        path = self.build / 'build.json'
        original = path.read_bytes()
        mutations = [lambda v: v.update(build_tags=[]),
                     lambda v: v.update(build_tags=['tools', 'feature']),
                     lambda v: v.update(build_tags=['feature']),
                     lambda v: v.update(schema_version=4),
                     lambda v: v.update(command_evidence={}),
                     lambda v: v['commands'][-1]['argv'].append('-race'),
                     lambda v: v['commands'][-1].update(exit_code=True)]
        try:
            for mutate in mutations:
                value = copy.deepcopy(self.receipt)
                mutate(value)
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError), patch('subprocess.run', side_effect=AssertionError('no compiler')):
                    prepared.verify_build(self.build, self.policy)
        finally:
            path.write_bytes(original)
        for name in ('commands.json', 'command-0.stdout', 'command-0.stderr', 'inspection/inventory.json'):
            file = self.build / name
            before = file.read_bytes()
            try:
                file.write_bytes(before + b'changed')
                with self.assertRaises(ValueError):
                    prepared.verify_build(self.build, self.policy)
            finally:
                file.write_bytes(before)
        # Rehashing an altered log or inventory must not legitimise another tag recipe.
        for name, mutate in (
                ('commands.json', lambda value: value[-1]['argv'].remove('-tags=feature,tools,\u03c0')),
                ('commands.json', lambda value: value[-1].update(cwd=str(self.root))),
                ('inspection/inventory.json', lambda value: value.update(build_tags=['feature'])),
                ('inspection/inventory.json', lambda value: value.update(schema_version=1)),
                ('inspection/inventory.json', lambda value: value.update(schema_version=True))):
            file = self.build / name
            before = file.read_bytes()
            value = json.loads(before)
            mutate(value)
            changed = copy.deepcopy(self.receipt)
            try:
                file.write_text(json.dumps(value))
                if name == 'commands.json':
                    changed['commands'] = value
                    changed['command_evidence'][name] = go_source.identity(file)
                else:
                    changed['inventory'] = go_source.identity(file)
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    prepared.verify_build(self.build, self.policy)
            finally:
                file.write_bytes(before)
                path.write_bytes(original)
        self.assertEqual(prepared.verify_build(self.build, self.policy)['receipt'], self.receipt)

    def test_pinned_compiler_refuses_invalid_unicode_tags_without_application_execution(self) -> None:
        for index, value in enumerate(('\U0001f600', 'e\u0301', 'a\u00b2')):
            output = self.base / ('invalid-unicode-' + str(index))
            with self.assertRaisesRegex(ValueError, 'tag'):
                go_build.build_project(self.root, '.', self.policy, output, self.tools,
                                       build_tags=(value,))
            self.assertFalse((output / 'build.json').exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(go_build.inputs(self.root), self.original)

    def line(self, process: subprocess.Popen) -> bytes:
        if not select.select([process.stdout], [], [], 10)[0]:
            self.fail('tagged application did not respond')
        return process.stdout.readline()

    def test_tagged_build_launches_compiler_free_with_all_startup_and_live_states(self) -> None:
        from otelc.prepared_python import stop_group
        capture = Capture()
        self.addCleanup(capture.close)
        for collection in (False, True):
            for emission in (False, True):
                name = str(collection) + str(emission)
                data = copy.deepcopy(self.policy.data)
                data['metrics'].update(collection=collection, emission=emission)
                data['resource']['service_name'] = 'tagged-' + name
                data['export'].update(endpoint=f'http://127.0.0.1:{capture.server.server_port}',
                                      interval_ms=100000, shutdown_timeout_ms=1000)
                runtime = self.base / ('runtime-' + name)
                with patch('subprocess.run', side_effect=AssertionError('launch must be compiler-free')):
                    prepared.prepare(Policy.parse(data), runtime, build_directory=self.build)
                    argv = prepared.command(runtime)
                process = subprocess.Popen(argv, env=dict(os.environ, PATH='/no-compilers'),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True, bufsize=0)
                self.addCleanup(stop_group, process)
                for stream in (process.stdin, process.stdout, process.stderr):
                    self.addCleanup(stream.close)
                self.assertEqual(self.line(process), b'ready\n')
                deadline = time.monotonic() + 3
                while not (runtime / 'go-application-start.json').exists():
                    if time.monotonic() > deadline:
                        self.fail('missing application identity')
                    time.sleep(.005)
                pid = prepared.read(runtime / 'go-application-start.json')['pid']
                endpoint = runtime / 'control.json'
                state = control.wait_ready(endpoint, pid)
                self.assertEqual((state['collection'], state['emission']), (collection, emission))
                for recording, exporting in ((True, True), (True, False), (False, True),
                                              (False, False), (True, True)):
                    control.request(endpoint, 'collection ' + ('on' if recording else 'off'))
                    state = control.request(endpoint, 'emission ' + ('on' if exporting else 'off'))
                    self.assertEqual(state['pid'], pid)
                    before = len(capture.metrics)
                    process.stdin.write(b'call\n')
                    process.stdin.flush()
                    self.assertEqual(self.line(process), b'42\n')
                    control.request(endpoint, 'flush')
                    self.assertEqual(len(capture.metrics), before + int(recording and exporting))
                process.stdin.write(b'quit\n')
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertEqual((process.stdout.read(), process.stderr.read()), (b'', b''))
                final = prepared.execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=0)
                self.assertEqual(final['failures'], {})
                self.assertEqual((final['application_pid'], final['sdk_pid']), (pid, pid))
                self.assertFalse(final['source_complete'])
                spans = [s for packet in capture.traces for resource in packet.resource_spans
                    if any(a.key == 'service.name' and a.value.string_value == 'tagged-' + name
                           for a in resource.resource.attributes)
                    for scope in resource.scope_spans for s in scope.spans]
                self.assertEqual([s.name for s in spans], ['example.com/tagged.workload'] * 5)
                calls = [sum(p.as_int for p in metric.sum.data_points)
                    for packet in capture.metrics for resource in packet.resource_metrics
                    if any(a.key == 'service.name' and a.value.string_value == 'tagged-' + name
                           for a in resource.resource.attributes)
                    for scope in resource.scope_metrics for metric in scope.metrics
                    if metric.name == 'otelc.function.calls']
                self.assertEqual(calls[-1], 3)
        self.assertEqual(go_build.inputs(self.root), self.original)
