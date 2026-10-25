"""The unchanged common policy drives generated Go SDK settings."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from otelc import go_runtime_config as runtime
from otelc.config import Policy


class GoRuntimeConfigTests(unittest.TestCase):
    def policy(self) -> Policy:
        return Policy.parse({'schema_version': 3, 'functions': {'include': ['*workload'], 'exclude': ['*excluded*']},
                             'lifetimes': {'enabled': True, 'include': ['*WorkItem']}})

    def test_translation_keeps_shared_policy_and_resolved_selections(self) -> None:
        policy = self.policy()
        value = runtime.configuration(policy, ('app.workload',), (('app.WorkItem', True),), 'c' * 64)
        self.assertEqual(value['functions'], ['app.workload'])
        self.assertEqual(value['lifetimes'], [{'name': 'app.WorkItem', 'resource': True}])
        for key in ('collection', 'emission'):
            self.assertEqual(value[key], policy.data['metrics'][key])
        self.assertEqual(value['policy_sha256'], hashlib.sha256(json.dumps(policy.data, sort_keys=True).encode()).hexdigest())
        self.assertEqual(value['boundaries'], policy.data['metrics']['histogram_boundaries_seconds'])
        self.assertEqual(value['selection_sha256'], 'c' * 64)
        self.assertEqual(value['http_context'], policy.data['context']['http'])

    def test_refuses_unselected_malformed_duplicate_or_disabled_bindings(self) -> None:
        policy = self.policy()
        for functions, lifetimes, digest in (
            (('excluded.workload',), (), 'a' * 64), (('workload', 'workload'), (), 'a' * 64),
            (('other',), (), 'a' * 64), ((1,), (), 'a' * 64), (None, (), 'a' * 64),
            ((), (('Other', True),), 'a' * 64), ((), (('WorkItem', 1),), 'a' * 64),
            ((), (('WorkItem', True), ('WorkItem', True)), 'a' * 64), ((), ((),), 'a' * 64),
            ((), (), 'A' * 64), ((), (), None),
        ):
            with self.subTest(functions=functions), self.assertRaises(ValueError):
                runtime.configuration(policy, functions, lifetimes, digest)
        foreign = Policy.parse({'schema_version': 3, 'languages': ['java']})
        disabled = Policy.parse({'schema_version': 3, 'lifetimes': {'enabled': False}})
        with self.assertRaises(ValueError):
            runtime.configuration(foreign, (), (), 'a' * 64)
        with self.assertRaises(ValueError):
            runtime.configuration(disabled, (), (('WorkItem', True),), 'a' * 64)

    def test_private_publication_hash_modes_and_existing_path_preservation(self) -> None:
        policy = self.policy()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'runtime'
            old_umask = os.umask(0o777)
            try:
                value = runtime.prepare(policy, directory, functions=('app.workload',),
                                        lifetimes=(('app.WorkItem', True),), selection_sha256='a' * 64)
            finally:
                os.umask(old_umask)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            config = directory / 'config.json'
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            original = config.read_bytes()
            self.assertEqual(hashlib.sha256(original).hexdigest(), value['environment']['OTELC_GO_CONFIG_SHA256'])
            self.assertEqual(value['health_version'], 4)
            with self.assertRaises(FileExistsError):
                runtime.prepare(policy, directory, functions=(), lifetimes=(), selection_sha256='a' * 64)
            self.assertEqual(config.read_bytes(), original)

    def test_ambient_sdk_options_and_oversized_config_refuse_before_output(self) -> None:
        policy = self.policy()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'runtime'
            with patch.dict(os.environ, {'OTEL_EXPORTER_OTLP_TIMEOUT': 'invalid'}), self.assertRaisesRegex(ValueError, 'ambient'):
                runtime.prepare(policy, directory, functions=(), lifetimes=(), selection_sha256='a' * 64)
            self.assertFalse(directory.exists())
            with patch.object(runtime, 'MAX_CONFIG_BYTES', 1), self.assertRaisesRegex(ValueError, 'byte bound'):
                runtime.prepare(policy, directory, functions=(), lifetimes=(), selection_sha256='a' * 64)
            self.assertFalse(directory.exists())
