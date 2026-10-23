"""RPC identity and late replies use real channels with a controlled peer."""

from __future__ import annotations

import os
import threading
import unittest

from otelc.python_handoff import HandoffReader
from otelc import python_sdk_io as io
from otelc.python_sdk_process import SdkChannel


class PythonSdkChannelTests(unittest.TestCase):
    def peer(self, respond):
        events, descriptor = os.pipe()
        replies, output = os.pipe()
        channel = SdkChannel(descriptor, replies, sdk_pid=123,
                             launch_id='1' * 32, policy_sha256='2' * 64)
        errors = []

        def run():
            reader = HandoffReader()
            try:
                while data := os.read(events, 4096):
                    for record in reader.push(data):
                        status = {'schema_version': 1, 'kind': 'otelc.python.sdk',
                            'sdk_pid': 123, 'pid': os.getpid(),
                            'launch_id': '1' * 32, 'policy_sha256': '2' * 64,
                            'collection': True, 'emission': True,
                            'sequence': record.get('id')}
                        for value in respond(record, status):
                            io.reply(output, value)
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                os.close(events)
                os.close(output)

        thread = threading.Thread(target=run)
        thread.start()

        def close():
            channel.close()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

        self.addCleanup(close)
        return channel

    def test_late_failed_reply_cannot_poison_the_next_successful_request(self) -> None:
        def respond(record, status):
            identity = record['id']
            if identity == 2:
                return []
            if identity == 3:
                return [{'id': 2, 'ok': False, 'status': status | {'sequence': 2}},
                        {'id': 3, 'ok': True, 'status': status}]
            return [{'id': identity, 'ok': True, 'status': status}]

        channel = self.peer(respond)
        self.assertEqual(channel.bind(os.getpid())['sequence'], 1)
        with self.assertRaises(TimeoutError):
            channel.request('status', timeout=.01)
        self.assertEqual(channel.request('status')['sequence'], 3)

    def test_reply_identity_flags_and_future_request_ids_are_refused(self) -> None:
        changes = ({'schema_version': True}, {'sdk_pid': 124}, {'pid': True},
                   {'launch_id': '3' * 32}, {'policy_sha256': '4' * 64},
                   {'collection': 1}, {'kind': 'wrong'})
        for change in changes:
            with self.subTest(change=change):
                channel = self.peer(lambda record, status, change=change: [
                    {'id': record['id'], 'ok': True, 'status': status | change}])
                pid = os.getpid()
                with self.assertRaises(ValueError):
                    channel.bind(pid)
        channel = self.peer(lambda record, status: [
            {'id': record['id'] + 1, 'ok': True, 'status': status}])
        pid = os.getpid()
        with self.assertRaisesRegex(ValueError, 'reply identity differs'):
            channel.bind(pid)

    def test_requests_cannot_rebind_or_forge_checkpoint_identity(self) -> None:
        channel = self.peer(lambda record, status: [
            {'id': record['id'], 'ok': True, 'status': status}])
        with self.assertRaises(ValueError):
            channel.request('status')
        with self.assertRaises(RuntimeError):
            channel.controls(collection=True, emission=True)
        pid = os.getpid()
        channel.bind(pid)
        with self.assertRaises(RuntimeError):
            channel.bind(pid)
        with self.assertRaises(ValueError):
            channel.request('shutdown')
        for value in (0, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                channel.request('status', timeout=value)
        with self.assertRaises(ValueError):
            channel.seal({'pid': 123})
        # None of the refused requests closed or changed the live channel.
        self.assertEqual(channel.request('status')['sequence'], 2)
