"""Exception labels are bounded UTF-8, without application formatting hooks."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

from otelc.python_runtime import exception_type


class ExceptionNameTests(unittest.TestCase):
    def test_utf8_byte_limit_preserves_valid_names_and_falls_back_for_invalid_names(self) -> None:
        cases = [('ordinary', 'ordinary'), ('é' * 512, 'é' * 512),
                 ('é' * 513, 'BaseException'), ('a' * 1024, 'a' * 1024),
                 ('a' * 1025, 'BaseException'), (chr(0xD800), 'BaseException'),
                 (chr(0xDFFF), 'BaseException'), ('bad' + chr(0xD800) + 'name', 'BaseException')]
        for name, expected in cases:
            with self.subTest(name=ascii(name)):
                error_type = type('ObservedError', (Exception,), {})
                error_type.__qualname__ = name
                error = error_type()
                self.assertEqual(exception_type(error), expected)

    def test_rejecting_a_large_name_does_not_allocate_its_encoded_copy(self) -> None:
        script = """import json, sys, tracemalloc
sys.path.insert(0, sys.argv[1])
from otelc.python_runtime import exception_type
class LargeError(Exception):
    pass
LargeError.__qualname__ = 'x' * 1_000_000
error = LargeError()
tracemalloc.start()
label = exception_type(error)
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
print(json.dumps({'label': label, 'peak_bytes': peak}))
"""
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script,
                                 str(Path(__file__).resolve().parents[1])],
                                capture_output=True, text=True, timeout=30, check=True)
        measured = json.loads(result.stdout)
        self.assertEqual(measured['label'], 'BaseException')
        self.assertLess(measured['peak_bytes'], 16 * 1024, measured)


if __name__ == '__main__':
    unittest.main()
