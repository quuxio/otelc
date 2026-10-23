"""Shared benchmark responses require actual JSON integer measurements."""

from __future__ import annotations

import io
import json
import unittest
from typing import Any
from unittest import mock

from otelc import benchmark


class BenchmarkResponseTests(unittest.TestCase):
    def batch(self, value: Any) -> dict[str, Any]:
        process = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO(json.dumps(value) + '\n'))
        with mock.patch.object(benchmark.select, 'select', return_value=([process.stdout], [], [])):
            return benchmark.batch(process, 1)

    def test_boolean_float_missing_and_nonobject_values_are_not_valid_measurements(self) -> None:
        valid = {'calls': 1, 'elapsed_ns': 100, 'checksum': 4145152}
        for name in valid:
            for replacement in (True, False, 1.0, '1', None):
                with self.subTest(name=name, value=replacement):
                    with self.assertRaisesRegex(ValueError, 'invalid application timing response'):
                        self.batch(valid | {name: replacement})
            with self.subTest(missing=name):
                with self.assertRaisesRegex(ValueError, 'invalid application timing response'):
                    self.batch({key: value for key, value in valid.items() if key != name})
        for value in (None, [], 'result', 1, True):
            with self.subTest(nonobject=value):
                with self.assertRaisesRegex(ValueError, 'invalid application timing response'):
                    self.batch(value)

    def test_valid_integers_preserve_raw_extra_fields_and_do_not_trust_a_response_pid(self) -> None:
        value = {'calls': 1, 'elapsed_ns': 100, 'checksum': 4145152, 'pid': -1}
        self.assertEqual(self.batch(value), value)
        for value in ({'calls': 2, 'elapsed_ns': 1, 'checksum': 1},
                      {'calls': 1, 'elapsed_ns': 0, 'checksum': 1},
                      {'calls': 1, 'elapsed_ns': -1, 'checksum': 1}):
            with self.assertRaisesRegex(ValueError, 'invalid application timing response'):
                self.batch(value)
