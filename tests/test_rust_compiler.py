"""Actual pinned compiler products and independent literal-record refusals."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from otelc import native_gate, rust_compiler


class RustToolRecordTests(unittest.TestCase):
    """Literal products test reader contracts, not genuine compiler admission."""
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / 'tools'
        self.output.mkdir()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.root_patch = mock.patch.object(rust_compiler, 'ROOT', self.source)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        names = ['source/native/rust/compiler/src/main.rs', 'source/native/rust/bridge/src/lib.rs',
                 'source/native/rust/compiler/Cargo.toml', 'source/native/rust/compiler/Cargo.lock',
                 'sysroot/bin/rustc', 'sysroot/bin/cargo', 'sysroot/lib/librustc_driver-fixture.dylib',
                 'tools/compiler/debug/otelc-rust-compiler', 'tools/bridge/release/libotelc_rust_bridge.rlib']
        for name in names:
            path = self.root / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text('literal fixture bytes\n')
        self.host = rust_compiler.HOSTS[(sys.platform, rust_compiler.platform.machine())]
        self.data = {'schema_version': 1, 'kind': 'otelc.rust.compiler-tools',
            'profile': rust_compiler.PROFILE, 'sysroot': str(self.root / 'sysroot'), 'host': self.host,
            'version': 'release: 1.98.1\nLLVM version: 22.1.8\nhost: ' + self.host + '\n',
            'sources': [native_gate.identity(p) for p in rust_compiler.sources()],
            'tools': [native_gate.identity(self.root / name) for name in names[4:7]],
            'products': [native_gate.identity(self.root / name) for name in names[7:]],
            'source_complete': False}

    def write(self) -> rust_compiler.Tools:
        encoded = json.dumps(self.data)
        (self.output / rust_compiler.MANIFEST).write_text(encoded)
        return rust_compiler.Tools(self.output, encoded)

    def test_fixed_products_read_without_executing_commands_and_have_no_mutable_alias(self) -> None:
        expected = self.write()
        with mock.patch.object(rust_compiler.subprocess, 'Popen', side_effect=AssertionError('no compiler in reader')):
            tools = rust_compiler.read(self.output)
            self.assertEqual(tools, expected)
            value = tools.manifest()
            value['products'].clear()
            self.assertEqual(len(tools.manifest()['products']), 2)

    def test_closed_typed_profile_refuses_missing_extra_foreign_or_changed_fields(self) -> None:
        for key, value in (('schema_version', True), ('profile', 'another-profile'), ('kind', 'another-kind'),
                           ('host', 'wrong-host'), ('version', 'release: 1.99.0\n'), ('sysroot', []),
                           ('source_complete', 0), ('sources', [])):
            original = self.data[key]
            self.data[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                rust_compiler.verify(self.write())
            self.data[key] = original
        self.data['extra'] = True
        with self.assertRaises(ValueError): rust_compiler.verify(self.write())
        del self.data['extra']
        del self.data['profile']
        with self.assertRaises(ValueError): rust_compiler.verify(self.write())
        with self.assertRaises(ValueError): rust_compiler.verify(None)

    def test_changed_product_or_tool_and_replaced_record_refuse(self) -> None:
        original = self.write()
        (self.output / rust_compiler.MANIFEST).write_text(original.encoded + '\n')
        with self.assertRaisesRegex(ValueError, 'record changed'): rust_compiler.verify(original)
        self.write()
        for name in ('compiler/debug/otelc-rust-compiler', '../sysroot/bin/rustc'):
            path = self.output / name
            path.write_text('substituted bytes\n')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'inputs or products'):
                rust_compiler.verify(original)
            path.write_text('literal fixture bytes\n')
            # Byte restoration still changes the original file facts.
            with self.assertRaises(ValueError): rust_compiler.verify(original)

    def test_changed_source_or_new_module_refuses(self) -> None:
        tools = self.write()
        path = self.source / 'native/rust/compiler/src/new.rs'
        path.write_text('fn newly_added() {}\n')
        with self.assertRaisesRegex(ValueError, 'source binding'): rust_compiler.verify(tools)

    def test_unsupported_host_existing_output_and_missing_rustup_refuse(self) -> None:
        with mock.patch.object(rust_compiler.platform, 'machine', return_value='unsupported'):
            with self.assertRaisesRegex(ValueError, 'requires macOS'): rust_compiler.build(self.root / 'new')
        with self.assertRaises(FileExistsError): rust_compiler.build(self.output)
        with self.assertRaisesRegex(ValueError, 'outside its source'):
            rust_compiler.build(self.source / 'native/rust/bad-output')
        with mock.patch.object(rust_compiler.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'install Rust'): rust_compiler.build(self.root / 'missing-tools')

    def test_foreign_compiler_version_is_refused_before_cargo_build(self) -> None:
        with mock.patch.object(rust_compiler, 'execute', side_effect=[
                str(self.root / 'sysroot/bin/rustc').encode(), b'release: 1.99.0\n']) as calls:
            with self.assertRaisesRegex(ValueError, 'exact Rust1.98.1'):
                rust_compiler.build(self.root / 'wrong-version')
            self.assertEqual(calls.call_count, 2)

    def test_cli_tool_build_does_not_require_application_policy(self) -> None:
        from otelc import __main__ as cli
        result = self.write()
        with mock.patch.object(sys, 'argv', ['quux-otelc', '--config', '/missing', 'rust-tools',
                                            '--output', str(self.output)]), \
                mock.patch.object(rust_compiler, 'build', return_value=result) as build, \
                mock.patch.object(cli.Policy, 'load', side_effect=AssertionError('no policy for tools')), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 0)
            self.assertEqual(json.loads(output.getvalue())['profile'], rust_compiler.PROFILE)
            build.assert_called_once_with(self.output)


class RustCompilerCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name).resolve()
        # These command tests run ordinary Python children, never compilers.
        space = mock.patch.object(rust_compiler.shutil, 'disk_usage',
                                  return_value=SimpleNamespace(free=rust_compiler.FLOOR * 2))
        space.start()
        self.addCleanup(space.stop)

    def test_actual_success_failure_and_literal_arguments_have_retained_channels(self) -> None:
        argument = 'literal;$HOME `echo no`'
        raw = rust_compiler.execute([sys.executable, '-c', 'import sys;print(sys.argv[1])', argument],
                                    self.output, 'success', rust_compiler.environment())
        self.assertEqual(raw, argument.encode() + b'\n')
        with self.assertRaisesRegex(ValueError, 'compile.stderr') as failure:
            rust_compiler.execute([sys.executable, '-c', 'import sys;sys.stderr.write("original failure");sys.exit(7)'],
                                  self.output, 'compile', rust_compiler.environment())
        self.assertEqual((self.output / 'compile.stderr').read_text(), 'original failure')
        self.assertEqual(json.loads((self.output / 'compile.result.json').read_bytes())['exit_code'], 7)
        self.assertIn('exit 7', str(failure.exception))
        self.assertIn('original failure', str(failure.exception))

    def test_failure_summary_retains_only_a_bounded_escaped_stderr_tail(self) -> None:
        script = 'import os;os.write(2,b"prefix"+b"x"*8192+b"\\xff\\nlast diagnostic\\x1b");raise SystemExit(3)'
        with self.assertRaises(ValueError) as failure:
            rust_compiler.execute([sys.executable, '-c', script], self.output,
                                  'large', rust_compiler.environment())
        message = str(failure.exception)
        self.assertIn('exit 3', message)
        self.assertIn('last diagnostic\\x1b', message)
        self.assertNotIn('prefix', message)
        self.assertNotIn('\x1b', message)
        self.assertNotIn('\n', message)
        self.assertLess(len(message), 4600)
        self.assertGreater((self.output / 'large.stderr').stat().st_size, 8192)

    def test_storage_floor_before_or_during_execution_refuses_and_reaps_owned_process(self) -> None:
        high, low = SimpleNamespace(free=rust_compiler.FLOOR * 2), SimpleNamespace(free=0)
        with mock.patch.object(rust_compiler.shutil, 'disk_usage', return_value=low):
            with self.assertRaisesRegex(ValueError, 'at least 8 GiB'):
                rust_compiler.execute(['never-started'], self.output, 'before', {})
        samples = itertools.chain((high, high), itertools.repeat(low))
        with mock.patch.object(rust_compiler.shutil, 'disk_usage', side_effect=samples):
            with self.assertRaisesRegex(ValueError, 'stopped below'):
                rust_compiler.execute([sys.executable, '-c', 'import time;time.sleep(30)'],
                                      self.output, 'during', rust_compiler.environment())
        final = json.loads((self.output / 'during.result.json').read_bytes())
        self.assertEqual(final['exit_code'], -9)
        with self.assertRaises(ProcessLookupError): os.kill(final['pid'], 0)

    def test_build_budget_refuses_and_reaps_its_owned_process(self) -> None:
        with mock.patch.object(rust_compiler.time, 'monotonic', side_effect=(0, 1201, 1202)):
            with self.assertRaisesRegex(ValueError, 'build budget'):
                rust_compiler.execute([sys.executable, '-c', 'import time;time.sleep(30)'],
                                      self.output, 'budget', rust_compiler.environment())
        final = json.loads((self.output / 'budget.result.json').read_bytes())
        self.assertEqual(final['exit_code'], -9)

    def test_environment_uses_explicit_tools_but_preserves_installation_locations(self) -> None:
        values = {'RUSTC': '/wrong/compiler', 'RUSTFLAGS': 'wrong flags', 'CARGO_BUILD_TARGET': 'wrong-target',
                  'OTELC_RUST_REQUEST': '/wrong/request', 'LLVM_PROFILE_FILE': '/wrong/profile',
                  'CARGO_HOME': '/operator/cache', 'RUSTUP_HOME': '/operator/toolchains', 'PATH': '/usr/bin'}
        with mock.patch.dict(os.environ, values, clear=True):
            self.assertEqual(rust_compiler.environment(), {key: values[key] for key in
                             ('CARGO_HOME', 'RUSTUP_HOME', 'PATH')})


class ActualRustCompilerToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        selected = os.environ.get('OTELC_RUST_TOOLS_TEST_OUTPUT')
        if selected:
            cls.output = Path(selected).resolve()
            cls.output.mkdir(mode=0o700)
        else:
            temporary = tempfile.TemporaryDirectory(prefix='otelc-rust-tools-')
            cls.addClassCleanup(temporary.cleanup)
            cls.output = Path(temporary.name).resolve()
        cls.tools = rust_compiler.build(cls.output / 'tools')

    def test_actual_compiler_inspects_unedited_modules_without_an_sdk(self) -> None:
        root = self.output / 'application'
        root.mkdir()
        (root / 'main.rs').write_text('mod work; fn main(){println!("{}",work::workload(3));}\n')
        (root / 'work.rs').write_text('pub fn workload(value:i32)->i32{value+7}\n')
        sources = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.glob('*.rs')}
        inventory = self.output / 'inspection.json'
        request = self.output / 'request.json'
        request.write_text(json.dumps({'schema_version': 1, 'mode': 'inspect', 'root': str(root),
            'entry': str(root / 'main.rs'), 'sources': sources, 'selected': [], 'output': str(inventory)}))
        tools = rust_compiler.verify(self.tools)
        rust_compiler.execute([tools['products'][0]['path'], str(root / 'main.rs'), '--edition=2024',
            '--crate-name', 'original', '--sysroot', tools['sysroot'], '-o', str(self.output / 'unexpected-app')],
            self.output, 'actual-inspect', rust_compiler.environment() | {'OTELC_RUST_REQUEST': str(request)}, cwd=root)
        value = json.loads(inventory.read_bytes())
        self.assertEqual(value['mode'], 'inspect')
        self.assertEqual(value['sources'], sources)
        self.assertIn('work.rs::work::workload', [row['identity'] for row in value['definitions']])
        self.assertFalse((self.output / 'unexpected-app').exists())
        self.assertEqual(sources, {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.glob('*.rs')})

    def test_actual_tool_products_recheck_without_a_rebuild(self) -> None:
        self.assertEqual(rust_compiler.read(self.tools.directory), self.tools)
        self.assertEqual(len(self.tools.manifest()['products']), 2)
