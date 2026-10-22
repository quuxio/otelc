"""Ordinary unchanged-language workloads share an independent checksum oracle."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from otelc.benchmark import checksum
from test_node_sdk import node_command

ROOT = Path(__file__).resolve().parent.parent


class NodeExampleTests(unittest.TestCase):
    def test_http_examples_are_the_actual_strict_microsoft_compiler_output(self) -> None:
        compiler = ROOT / 'node_modules/typescript'
        package = json.loads((compiler / 'package.json').read_text())
        source = ROOT / 'examples/typescript/HttpContextApp.ts'
        before = source.read_bytes()
        with tempfile.TemporaryDirectory(prefix='otelc-http-emit-', dir='/tmp') as temporary:
            subprocess.run([node_command(), str(compiler / package['bin']['tsc']),
                '--target', 'ES2022', '--module', 'NodeNext', '--strict', '--types', 'node',
                '--outDir', temporary, str(source)], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30)
            self.assertEqual((Path(temporary) / 'HttpContextApp.js').read_bytes(),
                             (ROOT / 'examples/javascript/HttpContextApp.js').read_bytes())
        self.assertEqual(source.read_bytes(), before)

    def run_example(self, application: Path) -> None:
        before = hashlib.sha256(application.read_bytes()).hexdigest()
        result = subprocess.run([node_command(), '--expose-gc', str(application)],
            input='1\n10\n257\nthrow\nlifecycle\ngc\nquit\n', text=True,
            capture_output=True, check=True, timeout=10)
        self.assertEqual(result.stderr, '')
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 6)
        for count, reply in zip((1, 10, 257), replies[:3]):
            self.assertEqual(reply['calls'], count)
            self.assertEqual(reply['checksum'], checksum(count))
            self.assertIs(type(reply['elapsed_ns']), int)
            self.assertGreater(reply['elapsed_ns'], 0)
        self.assertEqual(replies[3], {'exception': 'RangeError', 'message': 'negative seed'})
        self.assertEqual(replies[4], {'construction_failed': True, 'close_failed': True})
        self.assertEqual(replies[5], {'gc_requested': True})
        self.assertEqual(hashlib.sha256(application.read_bytes()).hexdigest(), before)

    def test_javascript_matches_the_common_workload_without_telemetry(self) -> None:
        self.run_example(ROOT / 'examples/javascript/BenchmarkApp.js')

    def test_actual_microsoft_compiler_output_matches_javascript_and_preserves_source(self) -> None:
        compiler = ROOT / 'node_modules/typescript'
        package = json.loads((compiler / 'package.json').read_text())
        self.assertEqual(package['version'], '7.0.2')
        command = [node_command(), str(compiler / package['bin']['tsc'])]
        version = subprocess.check_output([*command, '--version'], text=True, timeout=30)
        self.assertEqual(version.strip(), 'Version 7.0.2')
        source = ROOT / 'examples/typescript/BenchmarkApp.ts'
        before = source.read_bytes()
        with tempfile.TemporaryDirectory(prefix='otelc-ts-example-', dir='/tmp') as temporary:
            output = Path(temporary)
            (output / 'package.json').write_text('{"type":"module"}\n')
            subprocess.run([*command,
                '--target', 'ESNext', '--module', 'NodeNext', '--strict', '--types', 'node',
                '--outDir', str(output), str(source)], cwd=ROOT,
                capture_output=True, text=True, check=True, timeout=30)
            self.run_example(output / 'BenchmarkApp.js')
        self.assertEqual(source.read_bytes(), before)
