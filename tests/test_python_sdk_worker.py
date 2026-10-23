"""The same SDK worker entry point is covered with actual inherited pipes."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from otelc.config import Policy
from otelc.python_handoff import HandoffWriter
from otelc.python_sdk_child import main, checkpoint, lifetime_counts, Worker
from otelc.python_sdk_process import policy_digest
from test_java_integration import Capture
from test_python_sdk_protocol import metric, span


class PythonSdkWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.policy = Policy.parse({'schema_version': 3,
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 100000},
            'traces': {'enabled': True, 'sample_ratio': 1.0}})
        self.launch = '1' * 32

    def producer(self, writer, *, spans=0):
        return {'kind': 'checkpoint', 'status': {
            'pid': os.getpid(), 'launch_id': self.launch,
            'function_completions': 1, 'active_functions': 0,
            'runtime_losses': {}, 'handoff': writer.snapshot(),
            'span_observation': {'sampled_started': spans,
                                 'sampled_ended': spans, 'handed_off': spans}}}

    def execute(self, *, invalid=False, lost_reply=False):
        self.root.chmod(0o700)
        plan_path = self.root / 'plan.json'
        plan_path.write_text(json.dumps({'schema_version': 1,
            'launch_id': self.launch, 'policy': self.policy.data,
            'directory': str(self.root)}))
        plan_path.chmod(0o600)
        plan = os.open(plan_path, os.O_RDONLY)
        events, descriptor = os.pipe()
        replies, output = os.pipe()
        writer = HandoffWriter(descriptor)
        response = []

        def receive():
            while data := os.read(replies, 65536):
                response.append(data)

        reader_thread = None
        if lost_reply:
            os.close(replies)
        else:
            reader_thread = threading.Thread(target=receive)
            reader_thread.start()
        self.assertTrue(writer.send({'kind': 'hello', 'id': 1,
            'pid': os.getpid(), 'launch_id': self.launch}))
        self.assertTrue(writer.send({'kind': 'controls', 'collection': True, 'emission': True}))
        self.assertTrue(writer.send(metric()))
        if invalid:
            self.assertTrue(writer.send({'kind': 'unexpected'}))
        else:
            self.assertTrue(writer.send({'kind': 'span', 'span': span()}))
            self.assertTrue(writer.send({'kind': 'request', 'id': 2, 'command': 'status'}))
            self.assertTrue(writer.send({'kind': 'request', 'id': 3, 'command': 'flush'}))
            self.assertTrue(writer.send(self.producer(writer, spans=1)))
        writer.close()
        argv = ['sdk-worker', '--plan-fd', str(plan), '--events-fd', str(events),
                '--replies-fd', str(output)]
        # main owns and closes plan/events/output; this is the real entry point.
        try:
            with mock.patch('sys.argv', argv):
                result = main()
        finally:
            if reader_thread is not None:
                reader_thread.join(3)
                self.assertFalse(reader_thread.is_alive())
                os.close(replies)
        report = json.loads((self.root / 'python-sdk-status.json').read_text())
        return result, report, b''.join(response)

    def test_real_entrypoint_matches_checkpoint_and_actual_sdk_outcomes(self) -> None:
        code, report, data = self.execute()
        self.assertEqual((code, report['phase']), (0, 'closed'))
        self.assertEqual(report['policy_sha256'], policy_digest(self.policy))
        self.assertEqual(report['handoff_received'], 7)
        self.assertEqual(report['span_accounting']['received'], 1)
        self.assertEqual(report['traces']['exported_items'], 1)
        self.assertEqual(self.capture.calls(), 1)
        replies = [json.loads(line) for line in data.splitlines()]
        self.assertEqual([value['id'] for value in replies], [1, 2, 3])
        self.assertTrue(all(value['ok'] for value in replies))

    def test_disconnected_reply_does_not_stop_telemetry_drain(self) -> None:
        code, report, _ = self.execute(lost_reply=True)
        self.assertEqual((code, report['phase']), (0, 'closed'))
        self.assertEqual(report['reply_lost'], 3)
        self.assertEqual(report['traces']['exported_items'], 1)

    def test_invalid_input_does_not_acquire_a_synthetic_checkpoint(self) -> None:
        code, report, _ = self.execute(invalid=True)
        self.assertEqual((code, report['phase']), (1, 'failed'))
        self.assertEqual(report['protocol_errors'], 1)
        self.assertFalse(report['producer_checkpoint_available'])
        self.assertIsNone(report['producer'])

    def test_checkpoint_identity_loss_and_span_relations_are_strict(self) -> None:
        value = {'pid': 123, 'launch_id': self.launch, 'function_completions': 2,
                 'active_functions': 0, 'runtime_losses': {},
                 'handoff': {'attempted': 2, 'accepted': 2, 'lost': 0, 'deadline_ms': 50},
                 'span_observation': {'sampled_started': 1, 'sampled_ended': 1, 'handed_off': 1}}
        self.assertIs(checkpoint(value, 123, self.launch, 2), value)
        for key, changed in (('pid', 124), ('runtime_losses', []),
                             ('runtime_losses', {'loss': -1}),
                             ('handoff', {'attempted': 3, 'accepted': 2, 'lost': 0, 'deadline_ms': 50}),
                             ('span_observation', {'sampled_started': 0, 'sampled_ended': 1, 'handed_off': 1})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                checkpoint(value | {key: changed}, 123, self.launch, 2)
        with self.assertRaises(ValueError):
            checkpoint(value, 123, self.launch, 1)

    def test_lifetime_counts_keep_failure_and_censoring_fields_explicit(self) -> None:
        value = {key: 0 for key in ('admitted', 'gc_completed', 'resource_completed',
            'current_live', 'open_resources', 'tracked_instances', 'tracked_constructors',
            'failed_initialisations', 'failed_releases', 'censored', 'lost')}
        value['losses'] = {}
        lifetime_counts(value)
        for changes in ({'losses': []}, {'losses': {'capacity': -1}}, {'lost': 1}):
            with self.assertRaises(ValueError):
                lifetime_counts(value | changes)

    def test_worker_refuses_unknown_plans_and_prehello_records(self) -> None:
        plan = {'schema_version': 1, 'launch_id': self.launch,
                'policy': self.policy.data, 'directory': str(self.root)}
        for changes in ({'schema_version': True}, {'launch_id': 'wrong'},
                        {'policy': Policy.parse({'schema_version': 3, 'languages': ['java']}).data}):
            with self.assertRaises(ValueError):
                Worker(plan | changes, -1)
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            Worker(plan, -1)
        self.root.chmod(0o700)
        worker = Worker(plan, -1)
        with self.assertRaises(ValueError):
            worker.consume({'kind': 'metric'})
        worker.checkpoint = {}
        with self.assertRaises(ValueError):
            worker.consume({'kind': 'hello'})
