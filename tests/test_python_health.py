"""Python health exposes existing facts without switching or exporting metrics."""

from __future__ import annotations

import copy
import gc
import json
import os
from pathlib import Path
import tempfile
import unittest

from otelc import control, health, python_health
from otelc.config import Policy
from otelc.python_control import ControlServer
from otelc.python_runtime import PythonRuntime
from otelc.python_sdk_process import SdkProcess
from test_java_integration import Capture
import test_python_lifetime_components as lifetime_fixture


def sdk_status() -> dict:
    """Independent primitive fixture includes failure and pending observations."""
    return {'schema_version': 1, 'kind': 'otelc.python.sdk', 'pid': 123,
        'sdk_pid': 456, 'launch_id': 'a' * 32, 'policy_sha256': 'b' * 64,
        'phase': 'running', 'export_started': 3, 'export_succeeded': 1,
        'export_failed': 1, 'in_flight_exports': 1, 'flush_started': 4,
        'flush_failed': 2,
        'traces': {'started': 2, 'succeeded': 1, 'failed': 1, 'pending': 0,
                   'started_items': 7, 'exported_items': 4, 'failed_items': 3},
        'span_accounting': {'queue_pending': None, 'queue_dropped': None,
                            'queue_evidence': 'unavailable'}}


def presentation(sdk=None, **options) -> dict:
    return python_health.present(pid=123, sdk_pid=456, launch_id='a' * 32,
        policy_sha256='b' * 64, phase=options.pop('phase', 'running'),
        sdk=sdk_status() if sdk is None else sdk,
        application={'runtime_losses': {'handoff': 2}},
        sampled={'sampled_started': 8, 'sampled_ended': 7}, **options)


class PythonHealthShapeTests(unittest.TestCase):
    def test_exact_batches_and_loss_preserve_unknown_points_and_flush(self) -> None:
        value = presentation()
        self.assertEqual(value['trace_exports']['units_failed'], '3')
        self.assertEqual(value['metric_exports']['batches_pending'], '1')
        self.assertIsNone(value['metric_exports']['units_started'])
        self.assertEqual(value['metric_flush'], {'scope': 'exporter', 'attempts': '4', 'failed_attempts': '2'})
        self.assertEqual(value['lifecycle'], {'flush': 'unavailable', 'shutdown': 'unavailable'})
        self.assertEqual(value['span_accounting']['sampled_active'], '1')
        self.assertIsNone(value['span_accounting']['queue_dropped'])
        self.assertEqual(value['application']['runtime_losses']['handoff'], 2)
        self.assertFalse(value['source_complete'])

    def test_final_shutdown_is_separate_from_export_failures_and_app_exit(self) -> None:
        for completed, expected in ((True, 'succeeded'), (False, 'failed'), (None, 'unavailable')):
            sdk = sdk_status(); sdk['shutdown_completed'] = completed
            value = presentation(sdk, phase='final', application_exit=7, sdk_exit=2,
                                 failures={'entry': 'unverified'})
            self.assertEqual(value['lifecycle']['shutdown'], expected)
            self.assertEqual(value['trace_exports']['batches_failed'], '1')
            self.assertEqual((value['application_exit'], value['sdk_exit']), (7, 2))
            self.assertEqual(value['failures'], {'entry': 'unverified'})

    def test_unavailable_sdk_and_absent_app_observer_are_not_zero(self) -> None:
        value = python_health.present(pid=None, sdk_pid=None, launch_id=None,
            policy_sha256='b' * 64, phase='final', sdk=None, application=None, sampled=None)
        self.assertFalse(value['sdk_attached'])
        self.assertIsNone(value['trace_exports']['exporters_observed'])
        self.assertIsNone(value['span_accounting']['sampled_started'])
        self.assertIsNone(value['metric_flush']['attempts'])

    def test_malformed_identity_counters_and_delivery_claims_are_rejected(self) -> None:
        value = presentation()
        changes = [({'schema_version': True}), ({'language': 'go'}), ({'source_complete': True}),
            ({'pid': True}), ({'sdk_pid': -1}), ({'launch_id': 'z' * 32}),
            ({'policy_sha256': None}), ({'downstream_receipt': 'succeeded'}),
            ({'lifecycle': {'flush': 'succeeded', 'shutdown': 'succeeded'}}),
            ({'metric_flush': {}}), ({'span_accounting': {}}), ({'trace_exports': {}}),
            ({'application_exit': True}), ({'failures': []}), ({'application': []})]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                health.validate({**value, **change})
        for key in ('pid', 'sdk_pid', 'launch_id', 'policy_sha256'):
            sdk = sdk_status(); sdk[key] = 'changed'
            with self.subTest(identity=key), self.assertRaises(ValueError):
                presentation(sdk)
        for name, key, replacement in (('span_accounting', 'sampled_active', True),
                ('span_accounting', 'processors_observed', -1),
                ('trace_exports', 'units_failed', '01'),
                ('trace_exports', 'exporters_observed', True),
                ('metric_flush', 'attempts', -1), ('sdk', 'schema_version', True)):
            changed = copy.deepcopy(value); changed[name][key] = replacement
            with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                health.validate(changed)
        for count in (True, -1, '1'):
            sdk = sdk_status(); sdk['export_started'] = count
            with self.subTest(count=count), self.assertRaises(ValueError):
                presentation(sdk)
        for group, key in (('traces', 'started'), ('span_accounting', 'queue_pending')):
            sdk = sdk_status(); del sdk[group][key]
            with self.subTest(group=group, key=key), self.assertRaises(ValueError):
                presentation(sdk)
        sdk = sdk_status(); del sdk['traces']
        with self.assertRaises(ValueError):
            presentation(sdk)
        with self.assertRaises(ValueError):
            python_health.present(pid=1, sdk_pid=2, launch_id='a' * 32,
                policy_sha256='b' * 64, phase='running', sdk=[], application=None, sampled=None)
        with self.assertRaises(ValueError):
            python_health.present(pid=1, sdk_pid=2, launch_id='a' * 32,
                policy_sha256='b' * 64, phase='running', sdk=None, application=None, sampled={})

    def test_retained_large_health_file_and_original_v1_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            path = root / 'health.json'
            value = presentation(); value['failures'] = {'diagnostic': 'x' * 5000}
            path.write_text(json.dumps(value)); path.chmod(0o600)
            self.assertEqual(health.read(path), value)
            path.write_text('{"schema_version":1}' + ' ' * 5000)
            with self.assertRaises(ValueError):
                health.read(path)
            path.write_text('{"schema_version":2,"schema_version":2}')
            with self.assertRaises(ValueError):
                health.read(path)


class PythonLiveHealthTests(unittest.TestCase):
    def test_health_preserves_queued_gc_and_has_no_collection_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = Capture(); self.addCleanup(capture.close)
            policy = Policy.parse({'schema_version': 3,
                'sources': {'include': ['test_python_lifetime_components.py']},
                'lifetimes': {'enabled': True, 'include': ['*']},
                'metrics': {'emission': False}, 'traces': {'enabled': False},
                'export': {'endpoint': f'http://127.0.0.1:{capture.server.server_port}',
                           'interval_ms': 100000}})
            sdk = SdkProcess.start(policy, Path(directory) / 'sdk'); self.addCleanup(sdk.abort)
            runtime = PythonRuntime(policy, sdk.channel, root=Path(__file__).resolve().parent)
            self.addCleanup(runtime.close)
            helper = lifetime_fixture.PythonLifetimeComponentTests()
            helper.runtime = runtime; helper.tracker = runtime.lifetimes
            item = lifetime_fixture.Item(); helper.admit(item)
            del item; gc.collect()
            pending = runtime.lifetimes.pending_events()
            self.assertGreater(pending, 0)
            counts = runtime.channel.writer.snapshot()
            value = python_health.live(runtime)
            self.assertEqual(runtime.lifetimes.pending_events(), pending)
            self.assertEqual(value['application']['lifetime_events_pending'], pending)
            self.assertEqual(value['application']['lifetimes']['gc_completed'], 0)
            # Only the status RPC crosses the pipe; no metric/span GC records.
            self.assertEqual(runtime.channel.writer.snapshot()['accepted'], counts['accepted'] + 1)
            runtime.status()
            self.assertEqual(runtime.lifetimes.snapshot()['gc_completed'], 1)

    def test_health_uses_same_pid_preserves_switches_and_never_forces_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = Capture(); self.addCleanup(capture.close)
            policy = Policy.parse({'schema_version': 3,
                'metrics': {'collection': False, 'emission': False},
                'traces': {'enabled': True, 'sample_ratio': 1.0},
                'export': {'endpoint': f'http://127.0.0.1:{capture.server.server_port}',
                           'interval_ms': 100000, 'timeout_ms': 1000}})
            sdk = SdkProcess.start(policy, root / 'sdk'); self.addCleanup(sdk.abort)
            runtime = PythonRuntime(policy, sdk.channel); self.addCleanup(runtime.close)
            server = ControlServer(runtime, root); self.addCleanup(server.close)
            before = runtime.status()
            first = control.request(root / 'control.json', 'health')
            self.assertEqual(first['pid'], os.getpid())
            self.assertEqual(first['sdk_pid'], sdk.process.pid)
            self.assertEqual(first['metric_exports']['batches_started'], '0')
            self.assertEqual(first['span_accounting']['sampled_started'], '0')
            self.assertEqual(runtime.status(), before)
            self.assertFalse(capture.metrics)
            runtime.enter(1, 'app.workload'); runtime.leave(1, None)
            active = control.request(root / 'control.json', 'health')
            self.assertEqual(active['span_accounting']['sampled_ended'], '1')
            self.assertIsNone(active['span_accounting']['queue_dropped'])
            self.assertFalse(active['sdk']['collection']); self.assertFalse(active['sdk']['emission'])
            server.close(); runtime.close(); final = sdk.wait_report()
            value = python_health.present(pid=os.getpid(), sdk_pid=sdk.process.pid,
                launch_id=sdk.channel.launch_id, policy_sha256=sdk.channel.policy_sha256,
                phase='final', sdk=final, application=final['producer'],
                sampled=final['producer']['span_observation'], sdk_exit=sdk.process.returncode)
            self.assertEqual(value['lifecycle']['shutdown'], 'succeeded')
            self.assertEqual(value['span_accounting']['queue_dropped'], '0')
            self.assertEqual(value['trace_exports']['units_succeeded'], '1')


if __name__ == '__main__':
    unittest.main()
