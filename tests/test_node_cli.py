"""Public launches retain original inputs, application results and the SDK drain."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from otelc import control, node
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parent.parent


def calls_by_language(capture: Capture) -> dict[str, int]:
    """Read each language's latest cumulative counter without adding repeated exports."""
    result: dict[str, int] = {}
    for message in capture.metrics:
        for resource in message.resource_metrics:
            attributes = {item.key: item.value.string_value for item in resource.resource.attributes}
            values = [point.as_int for scope in resource.scope_metrics for metric in scope.metrics
                      if metric.name == 'otelc.function.calls' for point in metric.sum.data_points]
            if values:
                language = attributes['otelc.language']
                result[language] = max(result.get(language, 0), *values)
    return result


class NodeCliTests(unittest.TestCase):
    def test_version_probe_does_not_execute_app_preloads_or_remove_app_module_paths(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='otelc-preload-', dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            modules = root / 'module-path' / 'application-dependency'
            modules.mkdir(parents=True)
            (modules / 'package.json').write_text('{"type":"commonjs"}\n')
            (modules / 'index.js').write_text('module.exports = 7;\n')
            policy = root / 'otelc.toml'
            policy.write_text('schema_version = 3\n[sources]\ninclude = ["**"]\n'
                '[functions]\ninclude = ["*.workload"]\n[export]\n'
                f'endpoint = "http://127.0.0.1:{capture.server.server_port}"\n')
            for plain in (True, False):
                with self.subTest(plain=plain):
                    audit = root / ('plain-preloads.txt' if plain else 'instrumented-preloads.txt')
                    preload = root / ('plain-preload.cjs' if plain else 'instrumented-preload.cjs')
                    preload.write_text("require('node:fs').appendFileSync(" + json.dumps(str(audit))
                        + ", String(process.pid) + '\\n');\n")
                    entry = root / ('plain-app.cjs' if plain else 'instrumented-app.cjs')
                    entry.write_text("const fs = require('node:fs');\n"
                        'function workload(value) { return value + 1; }\n'
                        "console.log(JSON.stringify({value:workload(require('application-dependency')),\n"
                        'pid:process.pid,preloads:fs.readFileSync(' + json.dumps(str(audit))
                        + ",'utf8').trim().split('\\n').length}));\n")
                    options = '--require=' + str(preload)
                    environment = {**os.environ, 'NODE_OPTIONS': options, 'NODE_PATH': str(modules.parent)}
                    with patch.dict(os.environ, environment, clear=True):
                        node.executable()
                        self.assertFalse(audit.exists(), 'version probe executed the application preload')
                        self.assertEqual(os.environ['NODE_OPTIONS'], options)
                    runtime = root / ('plain' if plain else 'instrumented')
                    invocation = [sys.executable, '-m', 'otelc', '--config', str(policy), 'node',
                        '--language', 'javascript', '--entry', str(entry), '--root', str(root),
                        '--runtime', str(runtime), *(['--plain'] if plain else [])]
                    result = subprocess.run(invocation, cwd=ROOT, env=environment,
                        capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    response = json.loads(result.stdout)
                    self.assertEqual((response['value'], response['preloads']), (8, 1))
                    self.assertEqual(audit.read_text().splitlines(), [str(response['pid'])])

    def test_both_public_examples_share_the_existing_common_policy_and_checksum(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='otelc-cli-examples-', dir=ROOT / 'build') as temporary:
            policy = Path(temporary) / 'otelc.toml'
            contents = (ROOT / 'examples/otelc.toml').read_text()
            original_endpoint = 'endpoint = "http://127.0.0.1:4320"'
            self.assertEqual(contents.count(original_endpoint), 1)
            policy.write_text(contents.replace(original_endpoint,
                f'endpoint = "http://127.0.0.1:{capture.server.server_port}"'))
            for language, suffix in [('javascript', 'js'), ('typescript', 'ts')]:
                source = ROOT / 'examples' / language / ('BenchmarkApp.' + suffix)
                original = source.read_bytes()
                for plain in (True, False):
                    with self.subTest(language=language, plain=plain):
                        result = subprocess.run([sys.executable, '-m', 'otelc', '--config', str(policy),
                            'example', language, *(['--plain'] if plain else [])], cwd=ROOT,
                            input='10\nquit\n', capture_output=True, text=True, timeout=20)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        response = json.loads(result.stdout)
                        self.assertEqual((response['calls'], response['checksum']), (10, 41284864))
                        self.assertGreater(response['elapsed_ns'], 0)
                        self.assertEqual(source.read_bytes(), original)
                        if plain:
                            self.assertEqual(result.stderr, '')
                        else:
                            self.assertTrue(result.stderr.startswith('Control descriptor: '))
                            descriptor = Path(result.stderr.removeprefix('Control descriptor: ').strip())
                            self.assertFalse(descriptor.parent.exists(), 'example runtime must clean up after SDK drain')
            self.assertEqual(calls_by_language(capture), {'javascript': 10, 'typescript': 10})

    def test_cli_controls_the_same_original_application_from_startup_off(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='otelc-cli-live-', dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            entry = root / 'app.cjs'
            entry.write_text("const input = require('node:readline').createInterface({input:process.stdin});\n"
                'function workload(value) { return value + 1; }\n'
                "input.on('line', line => { if (line === 'quit') { input.close(); return; }\n"
                'console.log(JSON.stringify({value:workload(Number(line)),pid:process.pid})); });\n')
            original = entry.read_bytes()
            policy = root / 'otelc.toml'
            policy.write_text('schema_version = 3\n[sources]\ninclude = ["**"]\n'
                '[functions]\ninclude = ["*.workload"]\n'
                '[metrics]\ncollection = false\nemission = false\n'
                '[traces]\nenabled = true\nsample_ratio = 1.0\n'
                '[export]\ninterval_ms = 60000\ntimeout_ms = 2000\n'
                f'endpoint = "http://127.0.0.1:{capture.server.server_port}"\n')
            runtime = root / 'runtime'
            invocation = [sys.executable, '-m', 'otelc', '--config', str(policy), 'node',
                '--language', 'javascript', '--entry', str(entry), '--root', str(root), '--runtime', str(runtime)]
            with subprocess.Popen(invocation, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as launcher:
                try:
                    first = self.cli_batch(launcher)
                    application_pid = first['pid']
                    self.assertNotEqual(application_pid, launcher.pid)
                    descriptor = runtime / 'control.json'
                    control.wait_ready(descriptor, application_pid)
                    for collection, emission in [(False, False), (True, False), (False, True), (True, True)]:
                        control.request(descriptor, 'collection ' + ('on' if collection else 'off'))
                        state = control.request(descriptor, 'emission ' + ('on' if emission else 'off'))
                        self.assertEqual((state['collection'], state['emission']), (collection, emission))
                        self.assertEqual(self.cli_batch(launcher), {'value': 8, 'pid': application_pid})
                        control.request(descriptor, 'flush')
                        if not (collection and emission):
                            self.assertFalse(capture.metrics)
                    stdout, stderr = launcher.communicate('quit\n', timeout=20)
                    self.assertEqual((launcher.returncode, stdout), (0, ''))
                    self.assertEqual(stderr, f'Control descriptor: {runtime.resolve()}/control.json\n')
                finally:
                    if launcher.poll() is None:
                        launcher.kill()
                        launcher.communicate(timeout=5)
            report = json.loads((runtime / 'node-status.json').read_text())
            self.assertEqual(report['phase'], 'closed')
            self.assertEqual(report['pid'], application_pid)
            self.assertEqual(report['function_completions'], 5)
            self.assertEqual(capture.calls(), 2)
            self.assertEqual(entry.read_bytes(), original)
            self.assertFalse((runtime / 'control.json').exists())
            spans = [span for message in capture.traces for resource in message.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), 5)

    def cli_batch(self, launcher: subprocess.Popen[str]) -> dict[str, int]:
        self.assertIsNotNone(launcher.stdin)
        self.assertIsNotNone(launcher.stdout)
        launcher.stdin.write('7\n')
        launcher.stdin.flush()
        self.assertTrue(select.select([launcher.stdout], [], [], 10)[0], 'original app did not reply')
        return json.loads(launcher.stdout.readline())

    def test_original_js_and_both_microsoft_compilers_keep_arguments_exits_and_source(self) -> None:
        for language, suffix, backend in [('javascript', 'cjs', 'native'),
                                          ('typescript', 'cts', 'native'), ('typescript', 'cts', 'api')]:
            with self.subTest(language=language, backend=backend):
                self.check_launch(language, suffix, backend)

    def check_launch(self, language: str, suffix: str, backend: str) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix='otelc-cli-', dir=ROOT / 'build') as temporary:
            root = Path(temporary)
            entry = root / ('app.' + suffix)
            annotation = ': number' if language == 'typescript' else ''
            source = ('function workload(value' + annotation + ') { return value + 1; }\n'
                      'console.log(JSON.stringify({value:workload(Number(process.argv[2])),\n'
                      '  arguments:process.argv.slice(2),pid:process.pid}));\n'
                      'process.exitCode = Number(process.argv[5]);\n')
            entry.write_text(source)
            original = entry.read_bytes()
            policy = root / 'otelc.toml'
            policy.write_text('schema_version = 3\n'
                '[sources]\ninclude = ["**"]\n'
                '[functions]\ninclude = ["*.workload"]\n'
                '[traces]\nenabled = true\nsample_ratio = 1.0\n'
                '[export]\ninterval_ms = 60000\ntimeout_ms = 2000\n'
                f'endpoint = "http://127.0.0.1:{capture.server.server_port}"\n')
            arguments = ['7', 'argument with spaces', '--literal', '7']
            for plain in (True, False):
                runtime = root / ('plain' if plain else 'instrumented')
                invocation = [sys.executable, '-m', 'otelc', '--config', str(policy), 'node',
                    '--language', language, '--entry', str(entry), '--root', str(root),
                    '--runtime', str(runtime.relative_to(ROOT) if language == 'javascript' else runtime),
                    '--backend', backend, *(['--plain'] if plain else []),
                    '--', *arguments]
                result = subprocess.run(invocation, cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 7, result.stderr)
                application = json.loads(result.stdout)
                self.assertEqual(application['value'], 8)
                self.assertEqual(application['arguments'], arguments)
                self.assertEqual(entry.read_bytes(), original)
                if plain:
                    self.assertEqual(result.stderr, '')
                    self.assertFalse((runtime / 'node-status.json').exists())
                    self.assertFalse(capture.metrics)
                    self.assertFalse(capture.traces)
                    continue
                self.assertEqual(result.stderr, f'Control descriptor: {runtime.resolve()}/control.json\n')
                report = json.loads((runtime / 'node-status.json').read_text())
                self.assertEqual(report['phase'], 'closed')
                self.assertEqual(report['pid'], application['pid'])
                self.assertNotEqual(report['sdk_pid'], application['pid'])
                self.assertFalse((runtime / 'control.json').exists())
                self.assertEqual(report['span_accounting']['queue_dropped'], 0)
                self.assertEqual(capture.calls(), 1)
                spans = [span for message in capture.traces for resource in message.resource_spans
                         for scope in resource.scope_spans for span in scope.spans]
                self.assertEqual([span.name for span in spans], ['app.workload'])

    def test_failed_or_unavailable_sdk_drain_does_not_mask_an_application_failure(self) -> None:
        data = copy.deepcopy(Policy.load(ROOT / 'examples/otelc.toml').data)
        policy = Policy.parse(data)
        for application_exit in (0, 7):
            for report in ({'phase': 'failed'}, TimeoutError('controlled missing report')):
                with self.subTest(application_exit=application_exit, report=type(report).__name__):
                    with patch('otelc.node.command', return_value=['node', 'app']), \
                            patch('otelc.node.subprocess.call', return_value=application_exit), \
                            patch('otelc.node.Path.read_text', return_value='{}'), \
                            patch('otelc.node.node_provenance.finish', return_value=True), \
                            contextlib.redirect_stderr(io.StringIO()) as diagnostics:
                        if isinstance(report, Exception):
                            response = patch('otelc.node.wait_report', side_effect=report)
                        else:
                            response = patch('otelc.node.wait_report', return_value=report)
                        with response:
                            self.assertEqual(node.run(policy, Path('/tmp/retained-node'), 'javascript'),
                                             application_exit or 1)
                        self.assertIn('node-status.json', diagnostics.getvalue())
