"""Strict drop health versioning; literal reports do not prove runtime receipt."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from otelc import health
from test_health import report


def drop_report() -> dict:
    """Build a structurally complete internal Rust drop report."""
    functions = dict.fromkeys(('registered', 'started', 'ended', 'active',
                              'lost', 'invalid', 'metadata_errors'), '0')
    drops = dict.fromkeys(('types', 'admitted', 'completed', 'live', 'dropping',
                          'ending', 'censored', 'lost', 'duplicates', 'invalid'), '0')
    return report() | {'schema_version': 5, 'language': 'rust', 'source_complete': False,
        'function_accounting': functions | {'closed': True},
        'drop_accounting': drops | {'closing': True, 'closed': True}}


class RustDropHealthTests(unittest.TestCase):
    def test_explicit_version_is_read_live_and_after_exit_without_delivery_claims(self) -> None:
        value = drop_report()
        self.assertIs(health.validate(value), value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'health.json'
            for phase in ('running', 'final'):
                value['phase'] = phase
                path.write_text(json.dumps(value))
                path.chmod(0o600)
                self.assertEqual(health.read(path), value)

    def test_wrong_versions_languages_missing_and_foreign_profiles_refuse(self) -> None:
        value = drop_report()
        missing = value.copy(); del missing['drop_accounting']
        cases = [missing, *(value | {'schema_version': version} for version in (1, 2, 3, 4, 5.0, True)),
                 *(value | {'language': language} for language in ('java', 'c', 'cpp', 'python', 'go'))]
        cases.append(value | {'schema_version': 2, 'language': 'python'})
        for field in ('object_accounting', 'resource_accounting'):
            cases.append(value | {field: {}})
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                health.validate(candidate)

    def test_counters_flags_and_pending_boundary_cannot_be_coerced(self) -> None:
        original = drop_report()
        for key in original['drop_accounting']:
            for bad in (None, 0, True, '-1', '00', '1.0'):
                if key in ('closing', 'closed') and type(bad) is bool:
                    continue
                value = copy.deepcopy(original); value['drop_accounting'][key] = bad
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    health.validate(value)
            value = copy.deepcopy(original); del value['drop_accounting'][key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                health.validate(value)
        for key in ('destroying', 'releasing', 'unknown'):
            value = copy.deepcopy(original); value['drop_accounting'][key] = '0'
            with self.subTest(extra=key), self.assertRaises(ValueError):
                health.validate(value)


if __name__ == '__main__':
    unittest.main()
