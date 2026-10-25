"""Public format negotiation is separate from application/provider admission."""

from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import unittest
from unittest import mock

from otelc import prepared_python as prepared


class PythonProviderProtocolTests(unittest.TestCase):
    def test_closed_format_descriptor_keeps_unqualified_evidence_explicit(self) -> None:
        actual = prepared.protocols()
        self.assertEqual(actual, {
            'schema_version': 1, 'kind': 'otelc.python.protocols',
            'request': 1, 'plan': 1, 'prepared': 1, 'command': 1,
            'execution': 1,
            'journal': {'schema_version': 1, 'record_kinds': [1, 2, 3, 4]},
            'code': {'written': 2, 'readable': [2]},
            'health': 2, 'sdk': 1, 'code_comparisons': 1,
            'source_complete': False})
        self.assertIs(actual['source_complete'], False)
        self.assertEqual(json.loads(json.dumps(actual, allow_nan=False)), actual)

    def test_caller_mutation_cannot_change_the_next_handshake(self) -> None:
        original = prepared.protocols()
        changed = prepared.protocols()
        changed['journal']['record_kinds'].append(99)
        changed['code']['readable'].clear()
        changed['source_complete'] = True
        changed['execution'] = 99
        self.assertEqual(prepared.protocols(), original)

    def test_handshake_neither_opens_inputs_nor_builds_or_starts_processes(self) -> None:
        with ExitStack() as stack:
            for target in ('os.open', 'builtins.open', 'subprocess.Popen',
                           'subprocess.run', 'subprocess.check_output',
                           'otelc.prepared_python.tooling.build',
                           'otelc.prepared_python.tooling.toolchain',
                           'otelc.prepared_python.recipe.request',
                           'otelc.prepared_python.frontend.prepare'):
                stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
            for name in ('open', 'read_bytes', 'read_text', 'resolve', 'mkdir'):
                stack.enter_context(mock.patch.object(Path, name, side_effect=AssertionError(name)))
            self.assertEqual(prepared.protocols()['kind'], 'otelc.python.protocols')
            self.assertEqual(prepared.protocols(profile='python-native-origins-v2')['kind'], 'otelc.python.protocols')
            self.assertEqual(prepared.protocols(profile='python-native-cache-origins-v3')['kind'], 'otelc.python.protocols')


if __name__ == '__main__':
    unittest.main()
