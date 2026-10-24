"""Installed tooling is read-only even with independent application Go caches."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ci import build_go_upstream as tooling
from otelc import go_build, go_context_bundle as context, go_prepared_tools as installed, prepared_go
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class PreparedGoToolTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sdk = self.root / 'installed sdk'
        self.weaver = self.sdk / 'bin/otelc'
        self.compiler = self.root / 'compiler path/bin/go'
        self.bundle = self.sdk / 'bundle.tgz'
        for path, data in ((self.compiler, b'compiler'), (self.weaver, b'weaver'), (self.bundle, b'bundle')):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o700)
        bundle = patch.object(context, 'BUNDLE_SHA256', context.digest(b'bundle'))
        bundle.start(); self.addCleanup(bundle.stop)
        self.details = (str(self.weaver) + ': go1.27.3\n\tpath\t' + tooling.PACKAGE + '\n\tmod\t'
                        + context.UPSTREAM_MODULE + '\tv1.0.1\t' + context.MODULE_SUM + '\n')
        self.observed = {'GOROOT': str(self.compiler.parent.parent), 'GOVERSION': tooling.GO_VERSION,
                         'GOOS': 'linux', 'GOHOSTOS': 'linux', 'GOARCH': 'amd64', 'GOHOSTARCH': 'amd64'}
        self.tools = {'schema_version': 1, 'go_version': tooling.GO_VERSION, 'go': str(self.compiler),
            'go_sha256': context.digest(self.compiler.read_bytes()), 'platform': {'GOOS': 'linux', 'GOARCH': 'amd64'},
            'module': context.UPSTREAM_MODULE, 'version': context.UPSTREAM_VERSION,
            'module_sum': context.MODULE_SUM, 'module_go_sum': context.MODULE_GO_SUM,
            'weaver': str(self.weaver), 'weaver_sha256': context.digest(self.weaver.read_bytes()),
            'bundle': str(self.bundle), 'bundle_sha256': context.BUNDLE_SHA256, 'build_metadata': self.details}
        self.manifest = self.sdk / 'toolchain.json'
        self.write(self.tools)
        self.calls = []
        self.version = 'otelc version v1.0.1\n'

    def write(self, value: dict) -> None:
        self.manifest.write_text(json.dumps(value))

    def command(self, arguments: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append((arguments, kwargs))
        if arguments[1:3] == ['env', '-json']:
            result = json.dumps(self.observed)
        elif arguments[1:3] == ['version', '-m']:
            result = self.details
        else:
            self.assertEqual(arguments, [str(self.weaver), 'version'])
            result = self.version
        return subprocess.CompletedProcess(arguments, 0, result.encode(), b'')

    def prepare(self) -> installed.PreparedTools:
        with patch.object(installed.subprocess, 'run', side_effect=self.command):
            return installed.prepare(str(self.compiler), self.sdk)

    def test_only_read_queries_run_and_application_caches_do_not_select_installed_tools(self) -> None:
        def facts():
            return {str(path): installed.identity(path)[0] for path in self.sdk.rglob('*') if path.is_file()}
        before = facts()
        with patch.dict(os.environ, {'GOMODCACHE': str(self.root / 'application-cache'),
                                     'GOFLAGS': '-overlay=foreign', 'GOTOOLCHAIN': 'auto'}):
            bound = self.prepare()
        self.assertEqual(bound.toolchain(), self.tools)
        self.assertEqual(len(self.calls), 3)
        for _, options in self.calls:
            self.assertEqual(options['cwd'], self.sdk)
            self.assertEqual(options['env']['GOMODCACHE'], str(self.root / 'application-cache'))
            self.assertEqual((options['env']['GOFLAGS'], options['env']['GOTOOLCHAIN']), ('', 'local'))
        self.assertEqual(before, facts())
        with patch.object(installed.subprocess, 'run', side_effect=AssertionError('verification must not execute')):
            bound.verify(self.tools)
        view = bound.toolchain()
        view['platform']['GOOS'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'changed'):
            bound.verify(view)
        self.assertEqual(bound.toolchain(), self.tools)

    def test_wrong_manifest_pins_compiler_or_metadata_executables_refuse_before_execution(self) -> None:
        changes = [('schema_version', True), ('schema_version', 2), ('module_sum', 'wrong'),
                   ('module_go_sum', 'wrong'), ('bundle_sha256', 'wrong'), ('version', 'other'),
                   ('go_version', 'other'), ('go', '/other/compiler'), ('weaver', '/metadata/executable'),
                   ('bundle', 'relative/bundle'), ('build_metadata', None), ('extra', 1)]
        with patch.object(installed.subprocess, 'run', side_effect=AssertionError('no execution')):
            for key, value in changes:
                self.write(self.tools | {key: value})
                with self.subTest(key=key), self.assertRaises(ValueError):
                    installed.prepare(str(self.compiler), self.sdk)
            self.manifest.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                installed.prepare(str(self.compiler), self.sdk)

    def test_tool_bytes_facts_and_manifest_cannot_be_recertified_after_binding(self) -> None:
        for path in (self.compiler, self.weaver, self.bundle, self.manifest):
            bound = self.prepare()
            original = path.read_bytes()
            try:
                path.write_bytes(original + b'changed')
                with self.subTest(path=path), self.assertRaises(ValueError):
                    bound.verify(self.tools)
            finally:
                path.write_bytes(original)
        bound = self.prepare()
        self.weaver.write_bytes(self.weaver.read_bytes())
        with self.assertRaisesRegex(ValueError, 'changed'):
            bound.verify(self.tools)

    def test_wrong_native_version_root_platform_build_info_and_reported_version_refuse(self) -> None:
        for field, value in (('GOVERSION', 'go1.26.0'), ('GOROOT', '/other/root'),
                              ('GOOS', 'darwin'), ('GOHOSTARCH', 'arm64')):
            original = self.observed.copy()
            self.observed[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'native'):
                self.prepare()
            self.observed = original
        self.write(self.tools | {'platform': {}})
        with self.assertRaisesRegex(ValueError, 'native'):
            self.prepare()
        self.write(self.tools)
        original = self.details
        for details in (original + 'changed', original.replace(context.MODULE_SUM, 'wrong')):
            self.details = details
            with self.assertRaisesRegex(ValueError, 'metadata'):
                self.prepare()
        self.write(self.tools | {'build_metadata': self.details})
        with self.assertRaisesRegex(ValueError, 'metadata'):
            self.prepare()
        self.write(self.tools)
        self.details = original
        self.version = 'otelc version v0.1.0'
        with self.assertRaisesRegex(ValueError, 'metadata'):
            self.prepare()

    def test_missing_nonexecutable_changed_during_validation_and_failed_queries_refuse_without_logs(self) -> None:
        with patch.object(installed.shutil, 'which', return_value=None), self.assertRaisesRegex(ValueError, 'unavailable'):
            installed.prepare('absent', self.sdk)
        with patch.object(installed.shutil, 'which', return_value=str(self.compiler)), \
                patch.object(installed.subprocess, 'run', side_effect=self.command):
            self.assertEqual(installed.prepare('go', self.sdk).toolchain(), self.tools)
        self.weaver.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'executable'):
            self.prepare()
        self.weaver.chmod(0o700)
        for result in (subprocess.CompletedProcess([], 7, b'', b'failed'),
                       subprocess.CompletedProcess([], 0, b'x' * (1024 * 1024 + 1), b'')):
            with patch.object(installed.subprocess, 'run', return_value=result), self.assertRaisesRegex(ValueError, 'validation failed'):
                installed.prepare(str(self.compiler), self.sdk)
        self.assertFalse((self.sdk / 'go-tool-error.log').exists())
        def changed(arguments, **options):
            result = self.command(arguments, **options)
            if arguments == [str(self.weaver), 'version']:
                self.manifest.write_text(self.manifest.read_text())
            return result
        with patch.object(installed.subprocess, 'run', side_effect=changed), self.assertRaisesRegex(ValueError, 'changed'):
            installed.prepare(str(self.compiler), self.sdk)

    def test_negotiation_is_explicit_composes_with_tags_and_needs_no_tools_or_policy(self) -> None:
        legacy = prepared_go.protocols()
        self.assertNotIn('prepared_tools', legacy)
        self.assertEqual(prepared_go.protocols(prepared_tools=True), legacy | {'prepared_tools': 1})
        tagged = prepared_go.protocols(build_tags=True, prepared_tools=True)
        self.assertEqual(tagged, legacy | {'prepared_tools': 1, 'build': 5, 'inspection': 2})
        result = subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', '/no-policy',
            'go-protocols', '--build-tags', '--prepared-tools'], env=dict(os.environ, PATH='/no-tools',
            OTELC_PYTHON=sys.executable), capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), tagged)
        with self.assertRaisesRegex(ValueError, 'caller-held'):
            go_build.build_project(Path('/no-root'), '.', None, Path('/no-output'), {}, prepared_tools={})


class ActualPreparedGoToolTests(unittest.TestCase):
    def test_fresh_application_caches_preserve_readonly_installed_tools_for_build4_and5(self) -> None:
        from otelc.prepared_python import stop_group
        (ROOT / 'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='go-installed-', dir=ROOT / 'build') as temporary:
            base = Path(temporary).resolve()
            application = base / 'application sources'
            application.mkdir()
            (application / 'go.mod').write_text('module example.com/preparedtools\n\ngo 1.26.0\n')
            (application / 'main.go').write_text('''package main
import("fmt";"os")
func init(){if path:=os.Getenv("PREPARED_TOOL_APP_MARKER");path!=""{os.WriteFile(path,nil,0600)}}
func workload()int{return chosen()}
func main(){fmt.Println(workload())}
''')
            (application / 'default.go').write_text('//go:build !feature\n\npackage main\nfunc chosen()int{return 7}\n')
            (application / 'feature.go').write_text('//go:build feature\n\npackage main\nfunc chosen()int{return 42}\n')
            original = go_build.inputs(application)
            capture = Capture()
            self.addCleanup(capture.close)
            policy_path = base / 'otelc.toml'
            policy_path.write_text('schema_version=3\nlanguages=["go"]\n[sources]\ninclude=["**/*.go"]\n'
                '[functions]\ninclude=["*workload"]\n[traces]\nenabled=true\nsample_ratio=1.0\n'
                f'[export]\nendpoint="http://127.0.0.1:{capture.server.server_port}"\ninterval_ms=100000\n')
            policy = Policy.load(policy_path)
            sdk = ROOT / 'build/go-upstream'
            tools = tooling.build('go', sdk)
            self.assertEqual(installed.prepare(tools['go'], sdk).toolchain(), tools)
            cache = base / 'application cache'
            cache.mkdir()
            marker = base / 'application-initialiser-ran'
            environment = dict(os.environ, GOMODCACHE=str(cache / 'modules'), GOCACHE=str(cache / 'build'),
                               GOPATH=str(cache / 'path'), OTELC_PYTHON=sys.executable,
                               PREPARED_TOOL_APP_MARKER=str(marker))
            self.assertEqual(list(cache.iterdir()), [])
            paths = [sdk, *sdk.rglob('*')]
            modes = {path: path.stat().st_mode & 0o777 for path in paths}
            try:
                # Real OS permissions also catch an accidental installer invocation.
                for path in reversed(paths):
                    path.chmod(0o550 if path.is_dir() or path == sdk / 'bin/otelc' else 0o440)
                def tree():
                    return {str(path.relative_to(sdk)): installed.identity(path)[0]
                            for path in sdk.rglob('*') if path.is_file()}
                before = tree()
                for version, tags, expected in ((4, [], b'7\n'), (5, ['--build-tag', 'feature'], b'42\n')):
                    metric_start = len(capture.metrics)
                    output = base / ('build-' + str(version))
                    result = subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', str(policy_path),
                        'go-build', '--prepared-tools', '--go', tools['go'], '--root', str(application),
                        '--output', str(output), *tags], env=environment, capture_output=True, timeout=600)
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                    self.assertFalse(marker.exists())
                    receipt = json.loads((output / 'build.json').read_bytes())
                    self.assertEqual(receipt['schema_version'], version)
                    self.assertEqual(receipt['toolchain'], tools)
                    self.assertEqual(tree(), before)
                    self.assertEqual(go_build.inputs(application), original)
                    for plain in (True, False):
                        runtime = base / ('runtime-' + str(version) + '-' + str(plain))
                        prepared_go.prepare(policy, runtime, build_directory=output, plain=plain)
                        launched = subprocess.Popen(prepared_go.command(runtime), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                        self.addCleanup(stop_group, launched)
                        stdout, stderr = launched.communicate(b'', timeout=15)
                        self.assertEqual((launched.returncode, stdout, stderr), (0, expected, b''))
                        final = prepared_go.execution(runtime, supervisor_pid=launched.pid,
                                                      parent_pid=os.getpid(), returncode=0)
                        self.assertEqual(final['failures'], {})
                    calls = [point.as_int for packet in capture.metrics[metric_start:]
                        for resource in packet.resource_metrics for scope in resource.scope_metrics
                        for metric in scope.metrics if metric.name == 'otelc.function.calls'
                        for point in metric.sum.data_points]
                    self.assertTrue(calls, 'each woven build must emit decoded function calls')
                    self.assertEqual(set(calls), {1})
                spans = [span for packet in capture.traces for resource in packet.resource_spans
                         for scope in resource.scope_spans for span in scope.spans]
                self.assertEqual([span.name for span in spans], ['example.com/preparedtools.workload'] * 2)
                self.assertTrue(capture.metrics)
                self.assertEqual(tree(), before)
                # A concurrent metadata change after compilation must leave no success record.
                bound = installed.prepare(tools['go'], sdk)
                actual_inputs = go_build.inputs
                reads = 0
                def changed_inputs(root):
                    nonlocal reads
                    value = actual_inputs(root)
                    if root == application:
                        reads += 1
                        if reads == 2:
                            (sdk / 'toolchain.json').chmod(0o600)
                    return value
                failed = base / 'changed-tooling-build'
                with patch.object(go_build, 'inputs', side_effect=changed_inputs), \
                        patch.dict(os.environ, environment), self.assertRaisesRegex(ValueError, 'changed'):
                    go_build.build_project(application, '.', policy, failed, tools, prepared_tools=bound)
                self.assertTrue((failed / 'commands.json').exists())
                self.assertFalse((failed / 'build.json').exists())
            finally:
                for path in paths:
                    path.chmod(modes[path])
