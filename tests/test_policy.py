"""Common configuration conformance before any application can be launched."""

from __future__ import annotations

import copy
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from otelc.config import LANGUAGES, Policy, Selection, expression
from otelc.java import context_configuration, properties, validate_http_header_shape


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
        self.assertEqual(policy.data["context"], {"tasks": False, "http": False})
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
            {"context": {"tasks": 1}}, {"context": {"http": "true"}},
            {"context": {"unknown": True}},
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

    def test_common_context_flags_drive_only_the_declared_official_agent_modules(self) -> None:
        for tasks in (False, True):
            for http in (False, True):
                with self.subTest(tasks=tasks, http=http):
                    policy = Policy.parse({"schema_version": 3, "traces": {"enabled": True},
                                           "context": {"tasks": tasks, "http": http}})
                    values = context_configuration(policy)
                    self.assertEqual(values["otel.instrumentation.executors.enabled"], str(tasks).lower())
                    self.assertEqual(values["otel.instrumentation.java-http-client.enabled"], str(http).lower())
                    self.assertEqual(values["otel.instrumentation.java-http-server.enabled"], str(http).lower())
                    self.assertEqual(values["otel.instrumentation.otelc-http-headers.enabled"], str(http).lower())
                    self.assertEqual(values.get("otel.propagators"), "tracecontext,baggage" if http else None)
        for context in ({"tasks": True}, {"http": True}):
            with self.subTest(context=context):
                policy = Policy.parse({"schema_version": 3, "context": context})
                self.assertFalse(policy.data['traces']['enabled'])
                self.assertEqual(context_configuration(policy)['otel.instrumentation.java-http-client.enabled'],
                                 str(context.get('http', False)).lower())

    def test_http_preflight_refuses_missing_or_incompatible_jdk_headers_before_launch(self) -> None:
        declarations = ("private final java.net.http.HttpHeaders headers;\n"
                        "public java.net.http.HttpHeaders headers();")
        for result in (subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.CompletedProcess([], 1, declarations, "failure"),
                       subprocess.CompletedProcess([], 0, declarations.replace("final ", ""), "")):
            with self.subTest(result=result), patch("otelc.java.subprocess.run", return_value=result):
                with self.assertRaisesRegex(ValueError, "header layout"):
                    validate_http_header_shape()
        with patch("otelc.java.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, declarations, ""),
            subprocess.CompletedProcess([], 0, "private final java.net.http.HttpHeaders userHeaders;\n"
                "public java.net.http.HttpHeaders headers();\njava.net.http.HttpHeaders getUserHeaders();", "")]):
            validate_http_header_shape()

    def test_properties_escape_delimiters_newlines_and_unicode(self) -> None:
        self.assertEqual(properties({"b": "x=y", "a": "\n"}), "a=\\n\nb=x\\=y\n")
        self.assertIn("\\u00e9", properties({"name": "café"}))
        self.assertIn("\\ud83d\\ude00", properties({"name": "😀"}))


if __name__ == "__main__":
    unittest.main()
