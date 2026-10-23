"""Real monitoring and GC must not block on optional selection-cache work."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PythonCacheLockTests(unittest.TestCase):
    def test_real_gc_queue_and_monitoring_cache_overlap_does_not_deadlock(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            result = subprocess.run([sys.executable, str(ROOT / 'tests/fixtures/python_cache_gc_overlap.py'), temporary],
                cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)},
                capture_output=True, text=True, timeout=15)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, 'completed cache/GC overlap\n', ''), result.stderr)
