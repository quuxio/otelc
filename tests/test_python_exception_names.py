"""Exception labels are bounded UTF-8, without application formatting hooks."""
from __future__ import annotations

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


if __name__ == '__main__':
    unittest.main()
