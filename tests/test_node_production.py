"""A production installation must retain both actual Microsoft compiler backends."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci.check_node_production import check


class NodeProductionTests(unittest.TestCase):
    def test_production_install_contains_both_microsoft_compiler_profiles(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix='otelc-production-preload-', dir=root / 'build') as temporary:
            audit = Path(temporary) / 'preload-pids.txt'
            preload = Path(temporary) / 'preload.cjs'
            preload.write_text("require('node:fs').appendFileSync(" + json.dumps(str(audit))
                               + ", String(process.pid) + '\\n');\n")
            with patch.dict(os.environ, {'NODE_OPTIONS': '--require=' + str(preload),
                                         'NODE_PATH': str(Path(temporary) / 'app-modules')}):
                report = check()
            self.assertFalse(audit.exists(), 'packaging helpers executed application startup code')
        self.assertEqual(report['native_version'], '7.0.2')
        self.assertEqual(report['api_version'], '6.0.3')
        self.assertEqual(report['results'], {'native': 56, 'api': 56})
