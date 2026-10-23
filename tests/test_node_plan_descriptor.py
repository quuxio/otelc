"""The private SDK child accepts a prepared descriptor, never a CLI-selected file."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from otelc.node import executable

ROOT = Path(__file__).resolve().parent.parent


class NodePlanDescriptorTests(unittest.TestCase):
    def test_child_does_not_read_a_path_supplied_as_a_cli_argument(self) -> None:
        with tempfile.TemporaryDirectory(prefix='otelc-plan-argument-', dir=ROOT / 'build') as temporary:
            selected_file = Path(temporary) / 'private.txt'
            original = b'PRIVATE_CANARY'
            selected_file.write_bytes(original)
            selected_file.chmod(0o600)
            result = subprocess.run([executable(), str(ROOT / 'adapters/node/src/source-sdk-child.ts'),
                                     str(selected_file)], capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr, 'inherited private plan descriptor|EBADF')
            self.assertNotIn(original.decode(), result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(selected_file.read_bytes(), original)
