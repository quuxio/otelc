"""Failure diagnostics must survive temporary-directory cleanup without growth."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from node_launch_evidence import LIMIT, REPORTS, report, run_checked, segments
import test_node_execution_format as execution_format


def decode(value: dict) -> bytes:
    return b''.join(base64.b64decode(item['base64']) for item in value['segments'])


class NodeLaunchEvidenceTests(unittest.TestCase):
    def test_execution_fixture_captures_failed_sdk_report_before_cleanup(self) -> None:
        capture = SimpleNamespace(server=SimpleNamespace(server_port=12345))
        failed = subprocess.CompletedProcess(['fixture'], 1, b'module\n', b'Node SDK drain failed')
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
                'node_launch_evidence.subprocess.run', return_value=failed):
            root = Path(temporary)
            runtime = root / 'runtime'
            runtime.mkdir()
            status = b'{"phase":"failed","pid":123,"producer_checkpoint_available":false}'
            (runtime / 'node-status.json').write_bytes(status)
            entry = root / 'sample.mjs'
            entry.write_text('console.log("module");\n')
            with self.assertRaises(AssertionError) as raised:
                execution_format.NodeExecutionFormatTests().run_source(
                    root, entry, capture, 'javascript', 'native', False)
        self.assertFalse(root.exists())
        evidence = json.loads(str(raised.exception).split('Node launch failed: ', 1)[1])
        self.assertEqual(decode(evidence['reports']['node-status.json']), status)
        self.assertEqual(decode(evidence['launcher']['stdout']), b'module\n')

    def test_actual_failed_child_reports_survive_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = b'{"phase":"failed","producer_checkpoint_available":false}'
            journal = b'{"kind":"execution_started","pid":123}\n'
            (root / 'node-status.json').write_bytes(status)
            (root / 'node-sources.jsonl').write_bytes(journal)
            command = [sys.executable, '-c',
                       'import os;os.write(1,b"module\\n");os.write(2,b"drain\\xff");raise SystemExit(1)']
            with self.assertRaises(AssertionError) as raised:
                run_checked(command, cwd=root, runtime=root, timeout=5)
        self.assertFalse(root.exists())
        evidence = json.loads(str(raised.exception).split('Node launch failed: ', 1)[1])
        self.assertTrue(evidence['diagnostic_only'])
        self.assertEqual(evidence['launcher']['returncode'], 1)
        self.assertFalse(evidence['launcher']['timed_out'])
        self.assertEqual(decode(evidence['launcher']['stdout']), b'module\n')
        self.assertEqual(decode(evidence['launcher']['stderr']), b'drain\xff')
        self.assertEqual(decode(evidence['reports']['node-status.json']), status)
        self.assertEqual(decode(evidence['reports']['node-sources.jsonl']), journal)
        self.assertEqual(evidence['reports']['node-inputs.json'], {'unavailable': 'FileNotFoundError'})
        self.assertEqual(set(evidence['reports']), set(REPORTS))

    def test_large_files_and_channels_retain_only_bounded_head_and_tail(self) -> None:
        data = b'a' * LIMIT + b'middle' + b'z' * LIMIT
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'node-status.json'
            path.write_bytes(data)
            captured = report(path)
        for value in (segments(data), captured):
            self.assertTrue(value['truncated'])
            self.assertEqual(value['size'], len(data))
            self.assertEqual(decode(value), data[:LIMIT // 2] + data[-LIMIT // 2:])
            self.assertEqual([item['offset'] for item in value['segments']], [0, len(data) - LIMIT // 2])
        for size in (0, LIMIT - 1, LIMIT):
            self.assertEqual(decode(segments(data[:size])), data[:size])
            self.assertFalse(segments(data[:size])['truncated'])

    def test_timeout_is_a_failure_with_available_channels_and_reports(self) -> None:
        command = ['fixture']
        timeout = subprocess.TimeoutExpired(command, 40, output=b'partial', stderr=b'waiting')
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
                'node_launch_evidence.subprocess.run', side_effect=timeout):
            root = Path(temporary)
            with self.assertRaises(AssertionError) as raised:
                run_checked(command, cwd=root, runtime=root, timeout=40)
        evidence = json.loads(str(raised.exception).split('Node launch failed: ', 1)[1])
        self.assertTrue(evidence['launcher']['timed_out'])
        self.assertEqual(evidence['launcher']['timeout_seconds'], 40)
        self.assertEqual(decode(evidence['launcher']['stdout']), b'partial')
        self.assertEqual(decode(evidence['launcher']['stderr']), b'waiting')

    def test_success_reads_no_reports_and_preserves_text_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
                'node_launch_evidence.report', side_effect=AssertionError('unexpected report read')):
            root = Path(temporary)
            result = run_checked([sys.executable, '-c', 'print("module")'],
                                 cwd=root, runtime=root, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'module\n', ''))
