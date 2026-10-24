"""Original Go types and common selections drive source-free hook planning."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ci import build_go_upstream as tooling
from otelc import go_source
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]


class GoSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='go-source-', dir=ROOT / 'build')
        cls.directory = Path(cls.temporary.name).resolve()
        cls.root = cls.directory / 'original'
        cls.root.mkdir()
        (cls.root / 'app').mkdir()
        (cls.root / 'app/main.go').write_text('''package main
import "example.com/application/model"
func main(){model.New().Close()}
''')
        (cls.root / 'model').mkdir()
        (cls.root / 'model/model.go').write_text('''package model
type WorkItem struct {Values [2]uint64}
func New()*WorkItem{return &WorkItem{}}
func (o *WorkItem) Close()error{return nil}
func workload(input int)(value int,err error){return input,nil}
func excludedWorkload(){}
''')
        (cls.root / 'go.mod').write_text('module example.com/application\n\ngo 1.26.0\n')
        cls.tools = tooling.build('go', ROOT / 'build/go-upstream')
        cls.inventory = go_source.inspect_project(cls.root, './app', cls.tools, cls.directory / 'inspection')

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def policy(self) -> Policy:
        return Policy.parse({'schema_version': 3, 'languages': ['go'],
            'sources': {'include': ['**/*.go']},
            'functions': {'include': ['*workload', '*Workload'], 'exclude': ['*excluded*']},
            'lifetimes': {'enabled': True, 'include': ['*WorkItem'], 'release_methods': ['Close']}})

    def test_actual_package_types_and_policy_resolve_independent_hook_sets(self) -> None:
        plan = go_source.resolve(self.policy(), self.inventory)
        self.assertEqual([f['name'] for f in plan['functions']], ['example.com/application/model.workload'])
        self.assertEqual(plan['functions'][0]['results'][1]['failure'], 'err != nil')
        self.assertEqual([row['symbol'] for row in plan['types']], ['WorkItem'])
        self.assertEqual([(row['kind'], row['lifetime']) for row in plan['allocations']], [('composite', 0)])
        self.assertEqual([row['symbol'] for row in plan['releases']], ['Close'])
        self.assertFalse(plan['source_complete'])
        self.assertEqual(plan['context'], {'tasks': False, 'http': False})
        self.assertEqual(len(plan['sha256']), 64)
        for name, digest in self.inventory['files'].items():
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), digest)
        self.assertFalse((self.root / 'go.sum').exists())
        receipt = json.loads((self.directory / 'inspection/inspection.json').read_text())
        self.assertFalse(receipt['source_complete'])
        self.assertEqual(receipt['go']['sha256'], self.tools['go_sha256'])
        self.assertEqual(receipt['inventory']['sha256'], hashlib.sha256(
            (self.directory / 'inspection/inventory.json').read_bytes()).hexdigest())

    def test_source_exclusion_dominates_all_function_allocation_and_release_hooks(self) -> None:
        data = copy.deepcopy(self.policy().data)
        data['sources']['exclude'] = ['model/**']
        plan = go_source.resolve(Policy.parse(data), self.inventory)
        self.assertEqual([plan[key] for key in ('functions', 'types', 'allocations', 'releases')], [[], [], [], []])
        data = copy.deepcopy(self.policy().data)
        data['lifetimes']['exclude'] = ['*WorkItem']
        plan = go_source.resolve(Policy.parse(data), self.inventory)
        self.assertEqual(len(plan['functions']), 1)
        self.assertEqual([plan[key] for key in ('types', 'allocations', 'releases')], [[], [], []])

    def test_startup_metric_flags_do_not_change_compile_time_selection_identity(self) -> None:
        expected = go_source.resolve(self.policy(), self.inventory)['sha256']
        for collection in (False, True):
            for emission in (False, True):
                data = copy.deepcopy(self.policy().data)
                data['metrics'].update(collection=collection, emission=emission)
                self.assertEqual(go_source.resolve(Policy.parse(data), self.inventory)['sha256'], expected)
        data['context']['tasks'] = True
        self.assertNotEqual(go_source.resolve(Policy.parse(data), self.inventory)['sha256'], expected)

    def test_unsupported_selected_bindings_refuse_without_hiding_excluded_ones(self) -> None:
        for kind in ('functions', 'types', 'allocations'):
            inventory = copy.deepcopy(self.inventory)
            row = next(row for row in inventory[kind] if kind != 'functions' or row['symbol'] == 'workload')
            row['gap'] = 'unsupported shape'
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'unsupported selected Go'):
                go_source.resolve(self.policy(), inventory)
        for key, value in (('receiver_id', 'WorkItem'), ('receiver', '_'), ('generic', True),
                           ('parameters', 1), ('variadic', True), ('gap', 'unsupported'),
                           ('results', [{'error': False}]), ('results', [{}, {}])):
            inventory = copy.deepcopy(self.inventory)
            close = next(row for row in inventory['functions'] if row['symbol'] == 'Close')
            close[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'release method'):
                go_source.resolve(self.policy(), inventory)
        inventory = copy.deepcopy(self.inventory)
        inventory['functions'].append(next(row for row in inventory['functions'] if row['symbol'] == 'workload'))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            go_source.resolve(self.policy(), inventory)
        with self.assertRaisesRegex(ValueError, 'inventory'):
            go_source.resolve(self.policy(), {'schema_version': 2})
        with self.assertRaisesRegex(ValueError, 'not enabled'):
            go_source.resolve(Policy.parse({'schema_version': 3, 'languages': ['java']}), self.inventory)

    def test_module_and_output_boundaries_fail_before_application_build(self) -> None:
        empty = self.directory / 'empty'
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, 'module manifest'):
            go_source.inspect_project(empty, '.', self.tools, self.directory / 'unused')
        alias = self.directory / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'canonical'):
            go_source.inspect_project(alias, './app', self.tools, self.directory / 'unused')
        for name in ('go.work', 'vendor'):
            path = empty / name
            path.touch()
            with self.assertRaisesRegex(ValueError, 'workspace/vendor'):
                go_source.inspect_project(empty, '.', self.tools, self.directory / 'unused')
            path.unlink()
        with self.assertRaisesRegex(ValueError, 'outside original'):
            go_source.inspect_project(self.root, './app', self.tools, self.root / 'build')
        self.assertFalse((self.root / 'build').exists())


if __name__ == '__main__':
    unittest.main()
