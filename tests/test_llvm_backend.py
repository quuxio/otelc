"""Matched tools and caller-held plugin identities cannot be replaced by receipts."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from otelc import llvm_backend as backend
from otelc.config import Policy


class LLVMBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ('llvm-config', 'clang', 'clang++'):
            (self.root / name).write_text('fixed ' + name)
        self.source = self.root / 'pass.cpp'
        self.source.write_text('fixed source')
        self.header = self.root / 'function_abi.h'
        self.header.write_text('fixed ABI header')
        self.config = self.root / 'llvm-config'
        self.version = '22.1.8'
        self.compiler_version = '22.1.8'
        self.compile_code = 0
        self.change = None
        source = patch.object(backend, 'SOURCE', self.source)
        source.start(); self.addCleanup(source.stop)
        header = patch.object(backend, 'HEADER', self.header)
        header.start(); self.addCleanup(header.stop)
        run = patch.object(backend.subprocess, 'run', side_effect=self.fake_run)
        self.run_mock = run.start(); self.addCleanup(run.stop)

    def fake_run(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        if '-shared' in command:
            self.compilation = command
            if self.change:
                self.change()
            if not self.compile_code:
                Path(command[command.index('-o') + 1]).write_bytes(b'fixed plugin')
            return subprocess.CompletedProcess(command, self.compile_code, b'compiler output', b'compiler diagnostics')
        if command[-1] == '--version':
            value = self.version if Path(command[0]).name == 'llvm-config' else 'clang version ' + self.compiler_version
        elif command[-1] == '--bindir':
            value = str(self.root)
        elif command[-1] == '--cxxflags':
            value = '-I"/headers with spaces" -std=c++17'
        else:
            value = '-L"/libraries with spaces" -lLLVM-22'
        return subprocess.CompletedProcess(command, 0, value, '')

    def build(self, name: str = 'output', *, coverage: bool = False, registry: bool = False) -> backend.Plugin:
        return backend.build(self.root / name, config=self.config, coverage=coverage, function_registry=registry)

    def test_matched_build_keeps_literal_flags_and_all_input_product_identities(self) -> None:
        plugin = self.build(coverage=True)
        value = plugin.manifest()
        self.assertEqual(value['toolchain']['version'], '22.1.8')
        self.assertIn('-I/headers with spaces', self.compilation)
        self.assertIn('-L/libraries with spaces', self.compilation)
        self.assertIn('-fcoverage-mapping', self.compilation)
        self.assertEqual(value['headers'], [backend.identity(self.header)])
        self.assertEqual((value['profile'], value['probe_abi']), (backend.PROFILE, 'otelc.function.v1'))
        self.assertNotIn('-DOTELC_FUNCTION_REGISTRY_V1=1', self.compilation)
        database = json.loads((plugin.directory / 'compile_commands.json').read_text())
        self.assertEqual(database, [{'directory': str(backend.ROOT), 'file': str(self.source),
                                     'arguments': self.compilation}])
        self.assertEqual(self.run_mock.call_args.kwargs['cwd'], backend.ROOT)
        with patch.object(backend.subprocess, 'run', side_effect=AssertionError('verification executed metadata')):
            self.assertEqual(backend.verify(plugin), value)
        view = plugin.manifest()
        view['plugin']['sha256'] = 'changed'
        self.assertNotEqual(view, backend.verify(plugin))

    def test_source_profile_is_explicit_and_uses_the_common_source_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, 'registration'):
            backend.build(self.root / 'invalid-source', config=self.config, source_aware=True)
        plugin = backend.build(self.root / 'source-profile', config=self.config,
                               function_registry=True, source_aware=True)
        self.assertEqual((backend.verify(plugin)['profile'], plugin.manifest()['probe_abi']),
                         (backend.SOURCE_PROFILE, 'otelc.function.v2'))
        self.assertIn('-DOTELC_SOURCE_PROFILE_V1=1', self.compilation)
        policy = Policy.parse({'schema_version': 3, 'languages': ['c'],
                              'sources': {'include': ['**/?.c'], 'exclude': ['vendor/**']}})
        selected = backend.environment(policy, 'c', source_root=self.root)
        self.assertEqual(selected['OTELC_SOURCE_ROOT'], str(self.root))
        self.assertEqual(json.loads(selected['OTELC_SOURCE_INCLUDE']), ['**/?.c'])
        self.assertEqual(json.loads(selected['OTELC_SOURCE_EXCLUDE']), ['vendor/**'])
        self.assertNotIn('OTELC_SOURCE_ROOT', backend.environment(policy, 'c'))

    def test_build_command_verifies_before_printing_and_refuses_existing_output(self) -> None:
        output = self.root / 'command-output'
        arguments = ['--output', str(output), '--llvm-config', str(self.config)]
        with patch('sys.stdout', new_callable=io.StringIO) as printed:
            backend.main(arguments)
        self.assertEqual(json.loads(printed.getvalue())['plugin']['sha256'],
                         backend.identity(Path(json.loads(printed.getvalue())['plugin']['path']))['sha256'])
        with self.assertRaises(FileExistsError):
            backend.main(arguments)

    def test_changed_plugin_and_rehashed_receipt_still_refuse(self) -> None:
        plugin = self.build()
        value = plugin.manifest()
        path = Path(value['plugin']['path'])
        path.write_bytes(b'changed plugin')
        with self.assertRaisesRegex(ValueError, 'product'):
            backend.verify(plugin)
        value['plugin'] = backend.identity(path)
        (plugin.directory / backend.MANIFEST).write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            backend.verify(plugin)
        with self.assertRaisesRegex(ValueError, 'caller-held'):
            backend.verify(value)

    def test_changed_source_tool_or_audit_record_refuses(self) -> None:
        for name in ('source', 'header', 'clang', 'build.stdout', 'command.json', 'compile_commands.json'):
            plugin = self.build('output-' + name.replace('.', '-'))
            path = (self.source if name == 'source' else self.header if name == 'header'
                    else self.root / name if name == 'clang' else plugin.directory / name)
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.subTest(name=name), self.assertRaises(ValueError):
                backend.verify(plugin)
            path.write_bytes(original)

    def test_compile_failure_or_changed_inputs_never_publish_an_accepted_manifest(self) -> None:
        self.compile_code = 1
        with self.assertRaisesRegex(ValueError, 'compilation failed'):
            self.build('failed')
        self.assertFalse((self.root / 'failed' / backend.MANIFEST).exists())
        self.assertEqual((self.root / 'failed/build.stderr').read_bytes(), b'compiler diagnostics')
        self.compile_code = 0
        self.change = lambda: self.source.write_text('changed source')
        with self.assertRaisesRegex(ValueError, 'changed during compilation'):
            self.build('changed')
        self.assertFalse((self.root / 'changed' / backend.MANIFEST).exists())

    def test_wrong_llvm_compiler_and_coverage_input_refuse(self) -> None:
        for version in ('21.1.8', '22.1.8git', 'invalid'):
            self.version = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.build(version)
        self.version = '22.1.8'
        self.compiler_version = '22.1.7'
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.build('wrong-clang')
        with self.assertRaisesRegex(ValueError, 'boolean'):
            self.build(coverage=1)

    def test_registry_profile_is_explicit_and_header_changes_cannot_reuse_a_plugin(self) -> None:
        plugin = self.build(registry=True)
        value = backend.verify(plugin)
        self.assertEqual((value['profile'], value['probe_abi']),
                         (backend.REGISTERED_PROFILE, 'otelc.function.v2'))
        self.assertIn('-DOTELC_FUNCTION_REGISTRY_V1=1', self.compilation)
        with self.assertRaisesRegex(ValueError, 'registry'):
            self.build('invalid-registry', registry=1)
        self.header.write_text('changed header')
        with self.assertRaisesRegex(ValueError, 'product'):
            backend.verify(plugin)

    def test_default_tool_selection_respects_operator_paths(self) -> None:
        with patch.dict(backend.os.environ, {'LLVM_CONFIG': str(self.config)}, clear=True):
            self.assertEqual(backend.discover()['directory'], str(self.root))
        with patch.dict(backend.os.environ, {'OTELC_LLVM_BIN': str(self.root)}, clear=True):
            self.assertEqual(backend.discover()['directory'], str(self.root))
        with patch.dict(backend.os.environ, {}, clear=True), patch.object(Path, 'is_file', return_value=False), \
                patch.object(backend.shutil, 'which', return_value=None), self.assertRaisesRegex(ValueError, 'Install LLVM'):
            backend.discover()

    def test_nonregular_inputs_refuse_before_opening_them(self) -> None:
        fifo = self.root / 'fifo'
        os.mkfifo(fifo, 0o600)
        for path in (self.root, fifo):
            with self.subTest(path=path), patch.object(Path, 'open', side_effect=AssertionError('opened nonregular input')), \
                    self.assertRaisesRegex(ValueError, 'regular files'):
                backend.identity(path)

    def test_common_policy_controls_selection_not_live_metric_injection(self) -> None:
        data = {'schema_version': 3, 'languages': ['c', 'cpp'],
                'functions': {'include': ['Widget::operator[](?)', 'workload*'], 'exclude': ['*private*']}}
        policy = Policy.parse(data)
        with patch.dict(backend.os.environ, {'OTELC_FUNCTION_INCLUDE': 'bad', 'OTELC_LLVM_CPP': '1', 'KEPT': 'literal'}, clear=True):
            c = backend.environment(policy, 'c')
            cpp = backend.environment(policy, 'cpp')
            self.assertNotIn('OTELC_LLVM_CPP', c)
            self.assertEqual(cpp['OTELC_LLVM_CPP'], '1')
            self.assertEqual(json.loads(c['OTELC_FUNCTION_INCLUDE']), data['functions']['include'])
            self.assertEqual(json.loads(c['OTELC_FUNCTION_EXCLUDE']), data['functions']['exclude'])
            self.assertEqual(c['KEPT'], 'literal')
            changed = copy.deepcopy(data)
            changed['metrics'] = {'collection': False, 'emission': False}
            self.assertEqual(cpp, backend.environment(Policy.parse(changed), 'cpp'))
        with self.assertRaises(ValueError):
            backend.environment(policy, 'rust')
        with self.assertRaises(ValueError):
            backend.environment(Policy.parse({'schema_version': 3, 'languages': ['java']}), 'c')


if __name__ == '__main__':
    unittest.main()
