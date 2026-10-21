"""Common configuration conformance before any application can be launched."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from otelc.config import LANGUAGES, Policy, Selection, expression
from otelc.java import properties


class PolicyTests(unittest.TestCase):
    def test_one_example_policy_covers_every_target(self) -> None:
        policy = Policy.load(Path("examples/otelc.toml"))
        self.assertEqual(set(policy.data["languages"]), set(LANGUAGES))
        self.assertTrue(policy.selection("functions").accepts("examples.App.workload(int)"))
        self.assertFalse(policy.selection("functions").accepts("examples.App.excluded(int)"))
        self.assertTrue(policy.selection("lifetimes").accepts("examples.App$WorkItem"))

    def test_defaults_and_selection_precedence(self) -> None:
        policy = Policy.parse({"schema_version": 3})
        self.assertTrue(policy.data["metrics"]["collection"])
        self.assertTrue(policy.data["metrics"]["emission"])
        self.assertFalse(policy.selection("functions").accepts("anything"))
        select = Selection(("a.*",), ("a.secret",))
        self.assertTrue(select.accepts("a.work"))
        self.assertFalse(select.accepts("a.secret"))
        self.assertFalse(select.accepts("b.work"))

    def test_literal_name_punctuation_and_source_globs(self) -> None:
        selection = Selection(("A[foo]::method(?)",), ())
        self.assertTrue(selection.accepts("A[foo]::method(x)"))
        self.assertFalse(selection.accepts("Af::method(x)"))
        paths = Selection(("src/**/app?.py",), ("src/private/**",), True)
        self.assertTrue(paths.accepts("src/app1.py"))
        self.assertTrue(paths.accepts("src/pkg/app2.py"))
        self.assertFalse(paths.accepts("src/private/app1.py"))
        self.assertFalse(Selection(("src/*.py",), (), True).accepts("src/pkg/app.py"))
        self.assertEqual(expression("a/**", path=True), "^a/.*$")

    def test_invalid_settings_fail_before_resolution(self) -> None:
        base = Policy.load(Path("examples/otelc.toml")).data
        cases = [
            {"schema_version": True}, {"schema_version": 2}, {"unknown": {}},
            {"languages": []}, {"languages": ["java", "java"]}, {"languages": ["jvaa"]},
            {"sources": {"include": [""]}}, {"functions": {"include": "*"}},
            {"functions": {"exlcude": []}}, {"metrics": {"collection": 1}},
            {"export": {"interval_ms": 0}}, {"benchmark": {"samples": True}},
            {"export": {"shutdown_timeout_ms": 0}},
            {"lifetimes": {"max_live": 0}}, {"resource": {"service_name": ""}},
            {"resource": {"service_name": "bad\nname"}}, {"export": {"endpoint": 123}},
            {"export": {"endpoint": "file:///tmp/metrics"}},
            {"export": {"endpoint": "https://token@host/"}},
            {"traces": {"sample_ratio": float("nan")}},
            {"metrics": {"histogram_boundaries_seconds": []}},
            {"metrics": {"histogram_boundaries_seconds": [.1, .01]}},
            {"metrics": {"histogram_boundaries_seconds": [float("inf")]}},
        ]
        for case in cases:
            data = copy.deepcopy(base)
            for key, value in case.items():
                if isinstance(value, dict) and isinstance(data.get(key), dict):
                    data[key].update(value)
                else:
                    data[key] = value
            with self.subTest(case=case), self.assertRaises(ValueError):
                Policy.parse(data)

    def test_properties_escape_delimiters_newlines_and_unicode(self) -> None:
        self.assertEqual(properties({"b": "x=y", "a": "\n"}), "a=\\n\nb=x\\=y\n")
        self.assertIn("\\u00e9", properties({"name": "café"}))
        self.assertIn("\\ud83d\\ude00", properties({"name": "😀"}))


if __name__ == "__main__":
    unittest.main()
