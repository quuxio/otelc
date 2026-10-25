"""A post-validation pathname replacement cannot block witness metadata generation."""

from __future__ import annotations

import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class WitnessInputFileTests(unittest.TestCase):
    def test_post_validation_fifo_replacement_never_reopens_unbounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix='witness-input-', dir='/tmp') as temporary:
            script = Path(__file__).parent / 'fixtures/java_witness_reopen.py'
            with subprocess.Popen([sys.executable, '-u', str(script), temporary],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                try:
                    self.assertTrue(select.select([process.stdout], [], [], 20)[0], 'fixture setup did not finish')
                    first = process.stdout.readline()
                    if first != b'validated-binding-replaced-with-fifo\n':
                        out, err = process.communicate(timeout=3)
                        self.fail(repr((process.returncode, first + out, err)))
                    out, err = process.communicate(timeout=3)
                    self.assertEqual((process.returncode, first + out, err),
                        (0, b'validated-binding-replaced-with-fifo\nsource-change-refused-without-application-execution\n', b''))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
