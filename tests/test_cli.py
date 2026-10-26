"""Public dispatch and unsupported-profile errors."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from otelc.__main__ import main


class CliTests(unittest.TestCase):
    def test_benchmark_error_keeps_the_primary_message_and_cleanup_notes(self) -> None:
        failure = ValueError('measured checksum differs')
        failure.add_note('Node benchmark SDK drain also failed: OSError')
        stderr = io.StringIO()
        with patch('sys.argv', ['quux-otelc', 'benchmark', '--output', '/unused']), \
                patch('otelc.__main__.run_benchmark', side_effect=failure), contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as caught:
            main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(stderr.getvalue(), 'quux-otelc: measured checksum differs\n'
                         'quux-otelc: Node benchmark SDK drain also failed: OSError\n')

    def invoke(self, arguments: list[str]) -> str:
        output = io.StringIO()
        with patch("sys.argv", ["quux-otelc", *arguments]), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        return output.getvalue()

    def test_inspection_reports_current_and_future_adapters(self) -> None:
        result = json.loads(self.invoke(["plan"]))
        self.assertEqual(result["implemented"], ["java"])
        self.assertEqual(result["candidates"], ["typescript", "javascript", "python", "go"])
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
        with patch('otelc.go_benchmark.run', return_value=report) as run:
            self.invoke(['benchmark', '--language', 'go', '--build', '/tmp/go-build',
                         '--output', '/tmp/go-comparison', '--comparison-id', 'shared', '--no-publish'])
            self.assertEqual(str(run.call_args.kwargs['build_directory']), '/tmp/go-build')
            self.assertEqual(run.call_args.kwargs['comparison_id'], 'shared')
            self.assertFalse(run.call_args.kwargs['publish'])

    def test_go_benchmark_requires_its_build_and_other_languages_refuse_it(self) -> None:
        for arguments in (['--language', 'go'], ['--language', 'python', '--build', '/tmp/go-build']):
            with self.subTest(arguments=arguments), patch('sys.argv', [
                    'quux-otelc', 'benchmark', '--output', '/tmp/comparison', *arguments]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 1)

    def test_native_benchmarks_keep_their_sdk_selection_alongside_go(self) -> None:
        report = {'run_id': 'native', 'median_ns_per_operation': {'plain': 10}}
        for language in ('c', 'cpp'):
            with self.subTest(language=language), patch('otelc.native_benchmark.run', return_value=report) as run:
                self.invoke(['benchmark', '--language', language, '--sdk-build', '/tmp/native-sdk',
                             '--output', '/tmp/native-comparison', '--comparison-id', 'shared', '--no-publish'])
                self.assertEqual(run.call_args.kwargs['language'], language)
                self.assertEqual(str(run.call_args.kwargs['sdk_directory']), '/tmp/native-sdk')
                self.assertEqual(run.call_args.kwargs['comparison_id'], 'shared')
                self.assertFalse(run.call_args.kwargs['publish'])
