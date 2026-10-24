"""Policy-driven private Go compilation preserves source and module semantics."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ci import build_go_upstream as tooling
from otelc import go_build, go_runtime_config, health
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class GoBuildTests(unittest.TestCase):
    def test_actual_policy_build_records_unedited_app_and_automatic_allocation_release(self) -> None:
        tools = tooling.build('go', ROOT / 'build/go-upstream')
        with tempfile.TemporaryDirectory(prefix='go-build-', dir=ROOT / 'build') as temporary:
            directory = Path(temporary).resolve()
            original = directory / 'original'
            original.mkdir()
            (original / 'go.mod').write_text('module example.com/ordinary\n\ngo 1.26.0\n')
            (original / 'main.go').write_text('''package main
import ("fmt"; renamed "example.com/ordinary/model")
func identity[T any](value T)T{return value}
func workload()(int,error){item:=renamed.New();defer item.Close();return identity(int(item.Values[0])),nil}
func excluded(){panic("never invoked")}
func main(){value,err:=workload();fmt.Println(value,err)}
''')
            (original / 'model').mkdir()
            (original / 'model/object.go').write_text('''/* ordinary source */package model
type WorkItem struct{Values [2]uint64}
var global = &WorkItem{Values:[2]uint64{7,0}}
func New()*WorkItem{return &WorkItem{Values:[2]uint64{42,0}}}
func (item *WorkItem) Close()error{return nil}
''')
            before = go_build.inputs(original)
            capture = Capture()
            self.addCleanup(capture.close)
            policy = Policy.parse({'schema_version': 3, 'languages': ['go'],
                'sources': {'include': ['**/*.go']}, 'functions': {'include': ['*workload', '*identity']},
                'lifetimes': {'enabled': True, 'include': ['*WorkItem'], 'release_methods': ['Close']},
                'traces': {'enabled': True, 'sample_ratio': 1.0},
                'export': {'endpoint': f'http://127.0.0.1:{capture.server.server_port}', 'interval_ms': 100000}})
            output = directory / 'build'
            receipt = go_build.build_project(original, '.', policy, output, tools)
            selection = receipt['selection']
            self.assertEqual(len(selection['allocations']), 2)
            self.assertEqual(len(selection['functions']), 2)
            self.assertEqual(len(selection['releases']), 1)
            self.assertFalse(receipt['source_complete'])
            env = tooling.compiler_environment(tools, ROOT / 'build/go-upstream')
            plain = subprocess.run([receipt['plain']['path']], cwd=original, env=env,
                                   capture_output=True, timeout=10)
            runtime = go_runtime_config.prepare(policy, directory / 'runtime',
                functions=tuple(row['name'] for row in selection['functions']),
                lifetimes=tuple((row['name'], any(r['lifetime'] == index for r in selection['releases']))
                                for index, row in enumerate(selection['types'])),
                selection_sha256=selection['sha256'])
            process = subprocess.Popen([receipt['instrumented']['path']], cwd=original,
                env=env | runtime['environment'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual((plain.returncode, process.returncode), (0, 0), stderr)
            self.assertEqual(plain.stdout, b'42 <nil>\n')
            self.assertEqual((stdout, stderr), (plain.stdout, plain.stderr))
            self.assertEqual(go_build.inputs(original), before)
            report = health.read(directory / 'runtime/health.json')
            self.assertEqual(report['pid'], process.pid)
            self.assertEqual(report['controls']['pid'], process.pid)
            self.assertEqual(report['probe_accounting']['object_admitted'], '2')
            self.assertEqual(report['probe_accounting']['resource_released'], '1')
            spans = [span for packet in capture.traces for resource in packet.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), 3)
            self.assertEqual({span.name for span in spans},
                             {'example.com/ordinary.workload', 'example.com/ordinary.identity',
                              'example.com/ordinary/model.WorkItem lifetime'})
            function = next(span for span in spans if span.name == 'example.com/ordinary.workload')
            lifetime = next(span for span in spans if span.name.endswith(' lifetime'))
            self.assertFalse(lifetime.parent_span_id)
            self.assertEqual([(link.trace_id, link.span_id) for link in lifetime.links],
                             [(function.trace_id, function.span_id)])
            self.assertEqual(capture.calls(), 1)
            self.assertFalse((output / 'compiler-temp').exists())
            # A compiler-free second launch uses exactly the same frozen binary.
            changed = copy.deepcopy(policy.data)
            changed['metrics'].update(collection=False, emission=False)
            off = go_runtime_config.prepare(Policy.parse(changed), directory / 'off-runtime',
                functions=tuple(row['name'] for row in selection['functions']),
                lifetimes=(('example.com/ordinary/model.WorkItem', True),), selection_sha256=selection['sha256'])
            metrics_before = len(capture.metrics)
            again = subprocess.run([receipt['instrumented']['path']], cwd=original,
                                   env=env | off['environment'], capture_output=True, timeout=10)
            self.assertEqual((again.returncode, again.stdout, again.stderr), (0, plain.stdout, plain.stderr))
            self.assertEqual(len(capture.metrics), metrics_before)
            self.assertEqual(go_build.inputs(original), before)

    def test_actual_sdk_dependency_upgrade_is_refused_before_application_compilation(self) -> None:
        tools = tooling.build('go', ROOT / 'build/go-upstream')
        with tempfile.TemporaryDirectory(prefix='go-module-drift-', dir=ROOT / 'build') as temporary:
            root = Path(temporary).resolve()
            original = root / 'original'
            original.mkdir()
            (original / 'go.mod').write_text('module example.com/drift\n\ngo 1.26.0\n\nrequire golang.org/x/sys v0.46.0\n')
            (original / 'main.go').write_text('package main\nimport _ "golang.org/x/sys/unix"\nfunc main(){}\n')
            env = tooling.compiler_environment(tools, ROOT / 'build/go-upstream')
            subprocess.run([tools['go'], 'mod', 'download', 'golang.org/x/sys@v0.46.0'],
                           cwd=original, env=env, check=True, capture_output=True, timeout=60)
            before = go_build.inputs(original)
            policy = Policy.parse({'schema_version': 3, 'languages': ['go']})
            output = root / 'build'
            with self.assertRaisesRegex(ValueError, 'existing Go dependency: golang.org/x/sys'):
                go_build.build_project(original, '.', policy, output, tools)
            self.assertEqual(go_build.inputs(original), before)
            self.assertFalse((output / 'plain').exists())
            self.assertFalse((output / 'instrumented').exists())
            self.assertFalse((output / 'build.json').exists())

    def test_module_resolution_changes_and_language_settings_are_refused(self) -> None:
        settings = {'Go': '1.26.0', 'Toolchain': '', 'Godebug': None}
        before = go_build.documents(b'{"Path":"app","Main":true}\n{"Path":"dependency","Version":"v1.0.0","Sum":"fixed"}')
        after = copy.deepcopy(before) + [{'Path': 'new-sdk', 'Version': 'v1.47.0'}]
        go_build.preserve_modules(before, after, settings, settings)
        for key, value in (('Go', '1.27'), ('Toolchain', 'go1.27.3'), ('Godebug', [{'Key': 'panicnil', 'Value': '1'}])):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'language/toolchain/GODEBUG'):
                go_build.preserve_modules(before, after, settings, settings | {key: value})
        for key, value in (('Version', 'v1.1.0'), ('Sum', 'changed'), ('Replace', {'Path': 'other'})):
            changed = copy.deepcopy(after)
            changed[1][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'dependency'):
                go_build.preserve_modules(before, changed, settings, settings)
        with self.assertRaises(ValueError):
            go_build.preserve_modules(before, after[:1], settings, settings)
        for raw in (b'', b'{}', b'[]', b'{"Path":"one"}{"Path":"one"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                go_build.documents(raw)

    def test_nested_allocations_keep_evaluation_and_original_bytes(self) -> None:
        source = b'&Outer{Child:new(Inner)}'
        rows = [{'start': 0, 'end': len(source), 'lifetime': 0},
                {'start': 13, 'end': 23, 'lifetime': 1}]
        self.assertEqual(go_build.allocate(source, rows, 'hook'),
                         b'hookTrack(&Outer{Child:hookTrack(new(Inner), 1)}, 0)')
        with self.assertRaises(ValueError):
            go_build.allocate(source, [{'start': -1, 'end': 2, 'lifetime': 0}], 'hook')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'alias').symlink_to(root)
            with self.assertRaisesRegex(ValueError, 'symlinks'):
                go_build.inputs(root)
            policy = Policy.parse({'schema_version': 3, 'context': {'http': True}})
            with self.assertRaisesRegex(ValueError, 'pending library integration'):
                go_build.build_project(root, '.', policy, root.parent / 'unused', {})


if __name__ == '__main__':
    unittest.main()
