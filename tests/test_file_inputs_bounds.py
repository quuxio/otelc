"""Bounded metadata reads keep opened-file identity and normal hashing semantics."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from otelc import file_inputs


class BoundedInputTests(unittest.TestCase):
    def test_exact_limit_and_default_hashing_keep_the_same_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'input'
            content = b'x' * 32
            path.write_bytes(content)
            expected = file_inputs.identity(path)
            for retain in (False, True):
                actual, retained = file_inputs.checked_file(path, include_content=retain, max_bytes=32)
                self.assertEqual(actual, expected)
                self.assertEqual(retained, content if retain else b'')
                self.assertEqual(actual['sha256'], hashlib.sha256(content).hexdigest())
            path.write_bytes(content + b'x')
            with self.assertRaisesRegex(ValueError, 'exceeds its byte bound'):
                file_inputs.checked_file(path, include_content=True, max_bytes=32)

    def test_growth_after_open_is_bounded_and_always_closes_the_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'growing'
            path.write_bytes(b'{}')
            actual_fstat = os.fstat
            opened = []

            def grow_after_stat(descriptor: int) -> os.stat_result:
                info = actual_fstat(descriptor)
                opened.append(descriptor)
                if len(opened) == 1:
                    path.write_bytes(b'x' * 4096)
                return info

            with patch.object(file_inputs.os, 'fstat', side_effect=grow_after_stat):
                with self.assertRaisesRegex(ValueError, 'exceeds its byte bound'):
                    file_inputs.checked_file(path, include_content=True, max_bytes=32)
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):
                actual_fstat(opened[0])

    def test_path_replacement_cannot_substitute_for_the_opened_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'input'
            replacement = Path(temporary) / 'replacement'
            path.write_bytes(b'{}')
            replacement.write_bytes(b'[]')
            actual_fstat = os.fstat
            calls = []

            def replace_after_read(descriptor: int) -> os.stat_result:
                info = actual_fstat(descriptor)
                calls.append(descriptor)
                if len(calls) == 2:
                    os.replace(replacement, path)
                return info

            with patch.object(file_inputs.os, 'fstat', side_effect=replace_after_read):
                with self.assertRaisesRegex(ValueError, 'changed while reading'):
                    file_inputs.checked_file(path, include_content=True, max_bytes=32)
            self.assertEqual(path.read_bytes(), b'[]')
            self.assertEqual(len(calls), 2)

    def test_invalid_limits_fail_before_opening_an_input(self) -> None:
        for limit in (True, False, 0, -1, '32', 1.5):
            with self.subTest(limit=limit), patch.object(file_inputs.os, 'open') as opened:
                with self.assertRaisesRegex(ValueError, 'positive integer'):
                    file_inputs.checked_file(Path('unused'), max_bytes=limit)
                opened.assert_not_called()
