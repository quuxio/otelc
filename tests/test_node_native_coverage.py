"""Coverage tooling must preserve the ordinary loadable binary on every outcome."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci.check_node_native_coverage import check


class NodeNativeCoverageTests(unittest.TestCase):
    def exercise(self, percentage: float, *, profiles: bool = True, matching: bool = True) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            library = root / 'build/tools/otelc_node_observer-24.21.0.node'
            library.parent.mkdir(parents=True)
            library.write_bytes(b'ordinary loadable binary')

            def run(command: list[str], **kwargs: object) -> None:
                if command[1] == 'ci/build_node_observer.py':
                    library.write_bytes(b'instrumented binary')
                if command[1] == '--test' and profiles:
                    environment = kwargs['env']
                    assert isinstance(environment, dict)
                    filename = str(environment['LLVM_PROFILE_FILE']).replace('%p', '123').replace('%m', 'test')
                    Path(filename).write_bytes(b'profile')

            source = root / 'native/node/PromiseObserver.cpp' if matching else root / 'other.cpp'
            exported = {'data': [{'files': [{'filename': str(source),
                'summary': {'lines': {'count': 100, 'covered': percentage, 'percent': percentage}}}]}]}
            try:
                with patch('ci.check_node_native_coverage.ROOT', root), \
                        patch.dict(os.environ, {'OTELC_NODE': '/node', 'OTELC_LLVM_BIN': '/llvm'}, clear=True), \
                        patch('ci.check_node_native_coverage.subprocess.run', side_effect=run), \
                        patch('ci.check_node_native_coverage.subprocess.check_output', side_effect=['24.21.0\n', json.dumps(exported)]):
                    check()
            finally:
                self.assertEqual(library.read_bytes(), b'ordinary loadable binary')

    def test_exact_source_at_floor_is_accepted_and_ordinary_binary_restored(self) -> None:
        self.exercise(80.)

    def test_coverage_below_floor_is_rejected_and_ordinary_binary_restored(self) -> None:
        with self.assertRaisesRegex(ValueError, 'below 80'):
            self.exercise(79.9)

    def test_missing_actual_profiles_or_unrelated_source_cannot_establish_coverage(self) -> None:
        with self.assertRaisesRegex(ValueError, 'no LLVM profile'):
            self.exercise(100., profiles=False)
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            self.exercise(100., matching=False)
