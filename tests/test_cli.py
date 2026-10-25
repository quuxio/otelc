"""Public dispatch and unsupported-profile errors."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from otelc.__main__ import main


class CliTests(unittest.TestCase):
    def invoke(self, arguments: list[str]) -> str:
        output = io.StringIO()
        with patch("sys.argv", ["quux-otelc", *arguments]), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        return output.getvalue()

    def test_inspection_reports_current_and_future_adapters(self) -> None:
        result = json.loads(self.invoke(["plan"]))
        self.assertEqual(result["implemented"], ["java"])
        self.assertEqual(result["candidates"], ["typescript", "javascript", "python", "rust"])
        self.assertIn("rust", result["remaining"])

    def test_not_implemented_is_an_execution_error(self) -> None:
        with patch("sys.argv", ["quux-otelc", "example", "rust"]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 1)

    def test_python_candidacy_does_not_advertise_implementation(self) -> None:
        result = json.loads(self.invoke(["plan"]))
        self.assertIn("python", result["candidates"])
        self.assertIn("python", result["remaining"])
        self.assertNotIn("python", result["implemented"])

    def test_dispatches_the_actual_selected_operation(self) -> None:
        state = {"schema_version": 1, "pid": 123, "collection": False, "emission": False}
        with patch("otelc.control.request", return_value=state) as request:
            self.assertEqual(json.loads(self.invoke(["metrics", "--socket", "/tmp/example.sock", "off"])), state)
            self.assertEqual(request.call_args.args[1], "off")
            self.assertEqual(json.loads(self.invoke(["health", "--socket", "/tmp/example.sock"])), state)
            self.assertEqual(request.call_args.args[1], "health")
        with patch("otelc.health.read", return_value={"phase": "final"}) as read:
            self.assertEqual(json.loads(self.invoke(["health", "--report", "/tmp/health.json"])), {"phase": "final"})
            self.assertEqual(str(read.call_args.args[0]), "/tmp/health.json")
        with patch("otelc.java.command", return_value=["java", "app"]), patch("subprocess.call", return_value=0) as call:
            self.invoke(["example", "java", "--plain"])
            call.assert_called_once_with(["java", "app"])
        with patch("otelc.prepared_java.run", return_value=0) as run:
            self.invoke(["java", "--manifest", "app.json", "--runtime", "/tmp/otelc-run", "--plain", "--diagnostic-profile", "--", "--argument"])
            self.assertTrue(run.call_args.kwargs["plain"])
            self.assertTrue(run.call_args.kwargs["diagnostic_profile"])
            self.assertEqual(run.call_args.kwargs["arguments"], ("--argument",))
        with patch("otelc.node.run", return_value=0) as run:
            self.invoke(["node", "--language", "typescript", "--root", "/project",
                         "--entry", "/project/app.cts", "--runtime", "/tmp/node-run",
                         "--backend", "api", "--plain", "--", "--argument"])
            self.assertEqual(run.call_args.kwargs["arguments"], ("--argument",))
            self.assertEqual(run.call_args.kwargs["backend"], "api")
            self.assertTrue(run.call_args.kwargs["plain"])
            self.invoke(["example", "javascript", "--plain"])
            self.assertEqual(run.call_args.args[2], "javascript")
        report = {"run_id": "test", "median_ns_per_operation": {"plain": 10}}
        with patch("otelc.benchmark.run", return_value=report) as run:
            result = json.loads(self.invoke(["benchmark", "--output", "/tmp/bench", "--no-publish"]))
            self.assertEqual(result["run_id"], "test")
            self.assertFalse(run.call_args.kwargs["publish"])
        with patch("otelc.node_benchmark.run", return_value=report) as run:
            self.invoke(["benchmark", "--output", "/tmp/bench", "--language", "typescript",
                         "--backend", "api", "--comparison-id", "shared", "--no-publish"])
            self.assertEqual(run.call_args.kwargs["language"], "typescript")
            self.assertEqual(run.call_args.kwargs["backend"], "api")
            self.assertEqual(run.call_args.kwargs["comparison_id"], "shared")
