"""Bound actual private sockets, framing, refusal and shutdown independently."""
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import test_python_witness_state as state_tests
from test_python_witness_launch import Stream
from quux_otelc_python.witness_control import WitnessControl
from quux_otelc_python.witness_wire import MAX_FRAME_BYTES, encode_record


class PythonWitnessControlTests(unittest.TestCase):
    def fixture(self):
        folder = tempfile.TemporaryDirectory(prefix='wc-', dir='/private/tmp')
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        private = root / 'private'; private.mkdir(mode=0o700)
        state = state_tests.PythonWitnessStateTests().state()
        control = WitnessControl(str(private / 'w.sock'), state)
        self.addCleanup(control.close)
        return root, state, control

    def test_fragmented_and_coalesced_requests_and_half_close_keep_one_stream(self):
        _, state, control = self.fixture()
        stream = Stream(control.path); self.addCleanup(stream.socket.close)
        first = encode_record({'version': 1, 'kind': 'request', 'op': 'capabilities',
                               'request_id': '1' * 32, 'profile': 'python-sync-v1'})
        second = first.replace(b'1' * 32, b'2' * 32)
        stream.socket.sendall(first[:9]); stream.socket.sendall(first[9:] + second)
        self.assertTrue(stream.receive()['available'])
        self.assertTrue(stream.receive()['available'])
        stream.socket.shutdown(socket.SHUT_WR)
        state.invalidate('incomplete')
        self.assertEqual('invalidated', stream.receive()['event'])
        control.close()
        self.assertIsNone(stream.receive())
        self.assertFalse(control.worker.is_alive())
        self.assertFalse(control.path.exists())

    def test_malformed_partial_oversized_and_duplicate_requests_refuse_trusted_stream(self):
        for payload in (b'bad\n', b'{}\r\n', b'\xff\n', b'x' * MAX_FRAME_BYTES, b'{'):
            with self.subTest(payload=payload[:8]):
                _, state, control = self.fixture()
                stream = Stream(control.path); self.addCleanup(stream.socket.close)
                stream.socket.sendall(payload)
                if payload == b'{':
                    stream.socket.shutdown(socket.SHUT_WR)
                self.assertEqual('invalidated', stream.receive()['event'])
                self.assertIsNone(stream.receive())
                self.assertIsNotNone(state.fault)
                control.close()
        _, state, control = self.fixture()
        stream = Stream(control.path); self.addCleanup(stream.socket.close)
        frame = encode_record({'version': 1, 'kind': 'request', 'op': 'capabilities',
                               'request_id': '3' * 32, 'profile': 'python-sync-v1'})
        stream.socket.sendall(frame + frame)
        self.assertEqual('response', stream.receive()['kind'])
        self.assertEqual('duplicate_request', stream.receive()['reason'])
        self.assertIsNone(stream.receive())
        self.assertEqual(2, state.request_count)

    def test_request_budget_disconnect_and_unread_socket_cannot_hold_shutdown(self):
        _, state, control = self.fixture()
        stream = Stream(control.path); self.addCleanup(stream.socket.close)
        for index in range(16):
            stream.send('capabilities', profile='python-sync-v1')
            self.assertTrue(stream.receive()['available'])
        stream.send('capabilities', profile='python-sync-v1')
        self.assertEqual('request_capacity', stream.receive()['reason'])
        self.assertIsNone(stream.receive())
        self.assertEqual(16, state.request_count)
        control.close()
        for connected in ('absent', 'disconnected', 'open'):
            _, state, control = self.fixture()
            if connected != 'absent':
                stream = Stream(control.path); self.addCleanup(stream.socket.close)
                if connected == 'disconnected':
                    stream.socket.close()
                state.invalidate('incomplete')
            started = time.monotonic()
            control.close()
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(control.worker.is_alive())

    def test_private_permissions_existing_paths_and_replacements_are_preserved(self):
        root, _, control = self.fixture()
        unsafe = root / 'unsafe'; unsafe.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, '0700'):
            WitnessControl(str(unsafe / 'w.sock'), state_tests.PythonWitnessStateTests().state())
        occupied = control.path.parent / 'occupied'; occupied.write_text('keep')
        with self.assertRaises(OSError):
            WitnessControl(str(occupied), state_tests.PythonWitnessStateTests().state())
        self.assertEqual('keep', occupied.read_text())
        control.path.unlink(); control.path.write_text('replacement')
        control.close()
        self.assertEqual('replacement', control.path.read_text())

    def test_cleanup_permission_failure_invalidates_without_raising(self):
        _, state, control = self.fixture()
        stream = Stream(control.path); self.addCleanup(stream.socket.close)
        with patch.object(Path, 'lstat', side_effect=PermissionError):
            control.close()
        self.assertEqual('incomplete', state.fault)
        self.assertEqual('invalidated', stream.receive()['event'])
