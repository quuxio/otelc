"""A production installation must retain both actual Microsoft compiler backends."""

from __future__ import annotations

import unittest

from ci.check_node_production import check


class NodeProductionTests(unittest.TestCase):
    def test_production_install_contains_both_microsoft_compiler_profiles(self) -> None:
        report = check()
        self.assertEqual(report['native_version'], '7.0.2')
        self.assertEqual(report['api_version'], '6.0.3')
        self.assertEqual(report['results'], {'native': 56, 'api': 56})
