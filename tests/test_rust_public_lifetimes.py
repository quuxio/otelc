"""Public lifetime profile contracts; literal records are not compiler evidence."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from otelc import native_benchmark, native_launch, rust_build, rust_launch
from otelc.config import Policy
from test_native_benchmark import drain
from test_rust_public import policy
import test_rust_lifetimes as discovery_tests
import test_rust_public as public_tests


def lifetime_policy() -> Policy:
    """Use the same external TOML as the ordinary lifetime example."""
    return Policy.load(rust_build.ROOT / 'examples/rust-lifetimes.toml')


class RustLifetimeProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = lifetime_policy()

    def test_static_selectors_and_profile_change_while_runtime_limits_remain_separate(self) -> None:
        plain = policy()
        self.assertEqual(rust_build.profile(plain), rust_build.PROFILE)
        self.assertEqual(rust_build.modes(plain), ('inspect', 'weave'))
        rust_build.validate_policy(self.policy)
        self.assertEqual(rust_build.profile(self.policy), rust_build.LIFETIME_PROFILE)
        self.assertEqual(rust_build.modes(self.policy), ('inspect', 'lifetime-inspect', 'lifetime-weave'))
        selected = rust_build.selection(self.policy.data)
        for section, key, changed in (('metrics', 'collection', False), ('metrics', 'emission', False),
                                      ('lifetimes', 'max_live', 1)):
            data = copy.deepcopy(self.policy.data); data[section][key] = changed
            self.assertEqual(rust_build.selection(Policy.parse(data).data), selected)
        for key, changed in (('enabled', False), ('include', ['*Other']), ('exclude', ['*Item*'])):
            data = copy.deepcopy(self.policy.data); data['lifetimes'][key] = changed
            self.assertNotEqual(rust_build.selection(Policy.parse(data).data), selected)

    def test_woven_inventory_carries_complete_bound_bodies_and_no_completion_claim(self) -> None:
        inspected = {'schema_version': 1, 'kind': 'otelc.rust.compiler-inventory', 'mode': 'inspect',
                     'definitions': [], 'sources': {}, 'source_complete': False}
        binding = {'schema_version': 2, 'lifetimes': {'a.rs::workload': {'1': 'Item'}},
                   'lifetime_bodies': {'a.rs::workload': '1' * 64}}
        expected = rust_build.expected_woven(inspected, 'lifetime-weave', binding)
        self.assertEqual(expected, inspected | binding | {'mode': 'lifetime-weave',
            'kind': 'otelc.rust.lifetime-weave', 'instrumentation_complete': False})
        self.assertEqual(inspected['mode'], 'inspect')

    def test_public_discovery_refuses_historical_empty_and_unavailable_selections(self) -> None:
        fixture = discovery_tests.RustLifetimeSelectionTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.read()
        with self.assertRaisesRegex(ValueError, 'version-two'):
            rust_build.lifetime_bindings(fixture.path, fixture.policy, fixture.root, fixture.sources)
        fixture.read(fixture.version_two())
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            rust_build.lifetime_bindings(fixture.path, fixture.policy, fixture.root, fixture.sources)
        data = copy.deepcopy(fixture.policy.data)
        data['lifetimes']['include'] = ['*NoSuchType']
        empty = Policy.parse(data)
        with self.assertRaisesRegex(ValueError, 'matched no supported'):
            rust_build.lifetime_bindings(fixture.path, empty, fixture.root, fixture.sources)
        data['lifetimes']['include'] = ['*Item']
        selected = rust_build.lifetime_bindings(fixture.path, Policy.parse(data), fixture.root, fixture.sources)
        self.assertEqual(selected['schema_version'], 2)
        self.assertEqual(set(selected['lifetimes']), set(selected['lifetime_bodies']))
        self.assertTrue(selected['lifetimes'])

    def test_woven_inventory_rejects_changed_types_bodies_and_claimed_completion(self) -> None:
        fixture = public_tests.RustInputTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        bindings = {'schema_version': 2, 'lifetimes': {fixture.row['identity']: {'1': 'Item'}},
                    'lifetime_bodies': {fixture.row['identity']: '1' * 64}}
        expected = rust_build.expected_woven(fixture.inventory, 'lifetime-weave', bindings)
        path = fixture.base / 'woven.json'

        def read(value: dict) -> dict:
            path.write_text(json.dumps(value))
            return rust_build.inventory(path, fixture.root, fixture.sources, 'lifetime-weave', bindings)

        self.assertEqual(read(expected), expected)
        with self.assertRaisesRegex(ValueError, 'bindings differ'):
            rust_build.inventory(path, fixture.root, fixture.sources, 'lifetime-weave')
        for changed in ({'schema_version': 1}, {'schema_version': True}, {'lifetimes': {}},
                        {'lifetime_bodies': {}}, {'instrumentation_complete': True},
                        {'kind': 'otelc.rust.compiler-inventory'}, {'extra': 1}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                read(expected | changed)

    def test_start_uses_explicit_drop_plan_and_configured_bound_without_changing_parent_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve(); root = base / 'app'; root.mkdir()
            built = rust_build.Build(base / 'build', '{}')
            built.directory.mkdir()
            runtime = base / 'runtime'; metadata = {'root': str(root), 'selection_sha256': '1' * 64}
            observed = {}

            def spawn(command, *, cwd, env, pass_fds, **streams):
                observed.update(plan=json.loads(os.pread(pass_fds[0], 65536, 0)), command=command, environment=env)
                return Mock(pid=101)

            with patch.object(rust_build, 'verify', return_value=metadata), \
                    patch.object(rust_launch.subprocess, 'Popen', side_effect=spawn), \
                    patch.dict(rust_launch.os.environ, {'OTEL_TRACES_EXPORTER': 'foreign'}):
                child = rust_launch.start(built, self.policy, runtime, arguments=('literal two words',))
                self.assertEqual(rust_launch.os.environ['OTEL_TRACES_EXPORTER'], 'foreign')
            self.assertEqual(child.pid, 101)
            self.assertEqual(observed['plan']['schema_version'], 3)
            self.assertEqual(observed['plan']['settings']['drop_lifetimes'], {'enabled': True, 'max_live': 4096})
            self.assertEqual(observed['plan']['selection_sha256'], metadata['selection_sha256'])
            self.assertNotIn('OTEL_TRACES_EXPORTER', observed['environment'])
            self.assertEqual(observed['command'][-1], 'literal two words')

    def report(self) -> dict:
        value = drain()
        drops = value.pop('object_accounting'); drops['dropping'] = drops.pop('destroying')
        return value | {'schema_version': 5, 'language': 'rust', 'drop_accounting': drops,
            'policy_sha256': native_launch.digest(self.policy.data)}

    def test_drop_health_requires_bound_public_policy_and_loss_free_benchmark_drain(self) -> None:
        value = self.report()
        built = Mock(); built.manifest.return_value = {'selection_sha256': value['selection_sha256']}
        rust_launch.validate_health(value, built, self.policy, value['pid'])
        native_benchmark.validate_drain(value, self.policy)
        for field in ('live', 'dropping', 'ending', 'censored', 'lost', 'duplicates', 'invalid'):
            broken = copy.deepcopy(value); broken['drop_accounting'][field] = '1'
            with self.subTest(field=field), self.assertRaises(ValueError):
                native_benchmark.validate_drain(broken, self.policy)
        broken = copy.deepcopy(value); broken['drop_accounting']['completed'] = '0'
        with self.assertRaises(ValueError): native_benchmark.validate_drain(broken, self.policy)
        for current, candidate in ((policy(), value), (self.policy, value | {'schema_version': 1}),
                (self.policy, {key: item for key, item in value.items() if key != 'drop_accounting'})):
            with self.subTest(boundary='launch'), self.assertRaises(ValueError):
                rust_launch.validate_health(candidate, built, current, value['pid'])
            with self.subTest(boundary='drain'), self.assertRaises(ValueError):
                native_benchmark.validate_drain(candidate, current)


if __name__ == '__main__':
    unittest.main()
