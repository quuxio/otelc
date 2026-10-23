"""The cold public CLI must not run application startup hooks in tooling hosts."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import venv
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from otelc import cli_bootstrap as bootstrap
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class CliBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / 'application'
        self.app.mkdir()
        self.private = self.root / 'SDK environment'
        venv.EnvBuilder(with_pip=False, symlinks=True).create(self.private)
        self.python = self.private / 'bin/python'
        site = self.private / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
        site.mkdir(parents=True, exist_ok=True)
        # Use the same locked wheels, not a site hook which adds another env.
        for entry in Path(sysconfig.get_path('purelib')).iterdir():
            if entry.name == '__pycache__' or entry.suffix == '.pth' or entry.name.startswith('sitecustomize'):
                continue
            target = site / entry.name
            target.symlink_to(entry, target_is_directory=entry.is_dir())
        self.marker = self.root / 'startup-count'
        hook = ('import pathlib;print("installed startup");p=pathlib.Path('
                + repr(str(self.marker)) + ');p.write_text(p.read_text()+"x" if p.exists() else "x")\n')
        (site / 'application-startup.pth').write_text(hook)
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.config = self.root / 'otelc.toml'
        self.config.write_text(f'''schema_version=3
languages=["python"]
[sources]
include=["**/*.py"]
[functions]
include=["entry.workload"]
[metrics]
collection=false
emission=false
[traces]
enabled=true
sample_ratio=1.0
[export]
endpoint="http://127.0.0.1:{self.capture.server.server_port}"
interval_ms=100000
''')
        self.environment = {**os.environ, 'OTELC_PYTHON': str(self.python),
                            'PYTHONDONTWRITEBYTECODE': '1'}
        self.entry = self.app / 'entry.py'

    def invoke(self, arguments: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', str(self.config), *arguments],
            cwd=ROOT, env=self.environment, capture_output=True, timeout=30)

    def test_public_plan_and_prepare_only_are_clean_json_without_startup_or_app_side_effects(self) -> None:
        app_marker = self.app / 'executed'
        self.entry.write_text('from pathlib import Path\nPath("executed").touch()\n')
        plan = self.invoke(['plan'])
        self.assertEqual((plan.returncode, plan.stderr), (0, b''))
        self.assertIn('policy', json.loads(plan.stdout))
        directory = self.root / 'prepared'
        prepared = self.invoke(['python', '--root', str(self.app), '--entry', 'entry.py',
                               '--runtime', str(directory), '--prepare-only'])
        self.assertEqual((prepared.returncode, prepared.stderr), (0, b''))
        self.assertEqual(json.loads(prepared.stdout)['kind'], 'otelc.python.command')
        self.assertFalse(self.marker.exists())
        self.assertFalse(app_marker.exists())
        self.assertFalse((directory / 'sdk').exists())

    def test_plain_and_instrumented_public_launch_match_native_hook_bytes_counts_and_exits(self) -> None:
        self.entry.write_text('def workload(): return 42\nprint("original",workload())\nraise SystemExit(7)\n')
        for plain in (True, False):
            with self.subTest(plain=plain):
                self.marker.unlink(missing_ok=True)
                native = subprocess.run([str(self.python), 'entry.py'], cwd=self.app,
                    env=self.environment, capture_output=True, timeout=10)
                self.assertIn(b'installed startup\n', native.stdout)
                native_count = self.marker.read_text()
                self.assertTrue(native_count)
                self.marker.unlink()
                directory = self.root / ('plain' if plain else 'instrumented')
                arguments = ['python', '--root', str(self.app), '--entry', 'entry.py', '--runtime', str(directory)]
                if plain:
                    arguments.append('--plain')
                actual = self.invoke(arguments)
                self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                                 (native.returncode, native.stdout, native.stderr))
                self.assertEqual(self.marker.read_text(), native_count)
                receipt = json.loads((directory / 'python-execution.json').read_bytes())
                self.assertEqual(receipt['phase'], 'closed', receipt)
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual([span.name for span in spans], ['entry.workload'])

    def test_legacy_module_cli_is_refused_before_starting_an_app(self) -> None:
        marker = self.app / 'executed'
        self.entry.write_text('from pathlib import Path\nPath("executed").touch()\n')
        result = subprocess.run([str(self.python), '-m', 'otelc', '--config', str(self.config),
            'python', '--root', str(self.app), '--entry', 'entry.py', '--runtime', str(self.root / 'legacy')],
            cwd=ROOT, env=self.environment, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'bin/quux-otelc', result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / 'legacy').exists())

    def test_wrapper_does_not_run_a_bash_startup_file(self) -> None:
        marker = self.root / 'bash-ran'
        script = self.root / 'bash-env'
        script.write_text('touch ' + str(marker) + '\n')
        self.environment['BASH_ENV'] = str(script)
        result = self.invoke(['plan'])
        self.assertEqual((result.returncode, result.stderr), (0, b''))
        self.assertIn('policy', json.loads(result.stdout))
        self.assertFalse(marker.exists())

    def test_bootstrap_refuses_normal_interpreter_flags_and_derives_both_installation_schemes(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(bootstrap.main(), 2)
        self.assertIn('bin/quux-otelc', output.getvalue())
        with patch.object(bootstrap.sys, 'executable', str(self.python)):
            self.assertTrue(all(str(self.private) in path for path in bootstrap.sdk_paths()))
        with patch.object(Path, 'is_file', return_value=False), \
                patch.object(bootstrap.sysconfig, 'get_paths', return_value={'purelib': '/sdk', 'platlib': '/sdk'}):
            self.assertEqual(bootstrap.sdk_paths(), ('/sdk',))

    def test_isolated_dispatch_adds_only_explicit_paths_without_processing_site_hooks(self) -> None:
        import otelc.__main__ as commands
        host = SimpleNamespace(flags=SimpleNamespace(isolated=True, no_site=True),
                               path=['/stdlib', '/sdk'])
        with patch.object(bootstrap, 'sys', host), \
                patch.object(bootstrap, 'sdk_paths', return_value=('/sdk', '/native-sdk')), \
                patch.object(commands, 'main', return_value=17) as dispatch:
            self.assertEqual(bootstrap.main(), 17)
        self.assertEqual(host.path, [str(ROOT), '/stdlib', '/sdk', '/native-sdk'])
        dispatch.assert_called_once_with()
        self.assertFalse(self.marker.exists())
