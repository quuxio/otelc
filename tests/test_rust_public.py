"""Original-source Rust selection, retained records and real SDK execution."""
from __future__ import annotations

import copy
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ci.rust_compiler_cases import APPLICATION, WORK
from otelc import control, health, native_benchmark, native_build, native_gate, native_launch
from otelc import rust_build, rust_compiler, rust_launch, rust_benchmark
from otelc.config import Policy
from test_java_integration import Capture


def policy(endpoint: str = 'http://127.0.0.1:4320') -> Policy:
    """Use ordinary common selectors; no Rust instrumentation appears in the app."""
    return Policy.parse({'schema_version': 3, 'languages': ['rust'],
        'sources': {'include': ['**']}, 'functions': {'include': ['*workload*']},
        'traces': {'enabled': True, 'sample_ratio': 1.0},
        'export': {'endpoint': endpoint, 'interval_ms': 60000, 'timeout_ms': 1000,
                   'shutdown_timeout_ms': 5000}})


class RustInputTests(unittest.TestCase):
    """Literal input records exercise readers; they do not admit a compiler build."""
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'application'
        self.root.mkdir()
        self.source = self.root / 'app.rs'
        self.source.write_text('fn workload()->u32{7}\n')
        self.sources = {str(self.source): hashlib.sha256(self.source.read_bytes()).hexdigest()}
        self.row = {'identity': 'app.rs::module::workload', 'file': str(self.source),
                    'compiler_name': 'module::workload', 'start': 0, 'end': 21,
                    'asynchronous': False, 'expanded': False}
        self.inventory = {'schema_version': 1, 'kind': 'otelc.rust.compiler-inventory',
                          'mode': 'inspect', 'definitions': [self.row], 'sources': self.sources,
                          'source_complete': False}

    def read_inventory(self, value: object) -> dict:
        path = self.base / 'inventory.json'
        path.write_text(json.dumps(value))
        return rust_build.inventory(path, self.root, self.sources, 'inspect')

    def literal_build(self) -> dict:
        # These closed records are intentionally not genuine SDK/tool products.
        # Malformed top-level input must fail before either SDK reader is called.
        tools, sdk = self.base / rust_compiler.MANIFEST, self.base / native_build.SDK_MANIFEST
        tools.write_text('{}')
        sdk.write_text('{}')
        selected = policy().data
        return {'schema_version': 1, 'kind': 'otelc.rust.source-build', 'profile': rust_build.PROFILE,
            'root': str(self.root), 'entry': str(self.source), 'policy': selected,
            'policy_sha256': native_launch.digest(selected),
            'selection_sha256': native_launch.digest(rust_build.selection(selected)),
            'tools': native_gate.identity(tools), 'sdk': native_gate.identity(sdk),
            'inputs': [], 'requests': [], 'selected': [], 'optimisation': 2,
            'inventories': [], 'products': [], 'source_complete': False}

    def verify_literal(self, value: dict, current: Policy | None = None) -> None:
        encoded = json.dumps(value)
        built = rust_build.Build(self.base, encoded)
        with mock.patch.object(rust_build, 'checked_file', return_value=({}, encoded.encode())), \
                mock.patch.object(rust_compiler, 'read', side_effect=AssertionError('must refuse before SDK read')):
            rust_build.verify(built, current or policy())

    def test_public_compilation_pins_loader_to_the_verified_rust_sysroot(self) -> None:
        class InspectionReached(Exception):
            pass

        sysroot = self.base / 'selected-sysroot'
        tool_record = {'sysroot': str(sysroot), 'tools': [{'path': '/fixed/rustc'}],
                       'products': [{'path': '/fixed/driver'}]}
        tools = rust_compiler.Tools(self.base, json.dumps(tool_record))

        def execute(arguments, output, label, env, *, cwd):
            if label == 'discover':
                (output / 'original.d').write_text('original: app.rs\n')
                return b''
            self.assertEqual(label, 'inspect')
            self.assertEqual(env.get('LD_LIBRARY_PATH'), str(sysroot / 'lib'))
            raise InspectionReached

        with mock.patch.object(rust_compiler, 'read', return_value=tools), \
                mock.patch.object(native_build, 'sdk', return_value={}), \
                mock.patch.object(rust_compiler, 'execute', side_effect=execute), \
                mock.patch.object(rust_compiler.sys, 'platform', 'linux'), \
                mock.patch.dict(os.environ, {'LD_LIBRARY_PATH': '/foreign/libraries'}):
            with self.assertRaises(InspectionReached):
                rust_build.build(policy(), self.root, Path('app.rs'), self.base / 'build',
                                 tools_directory=self.base, sdk_directory=self.base)
            self.assertEqual(os.environ['LD_LIBRARY_PATH'], '/foreign/libraries')

    def test_dependency_escapes_preserve_spaces_unicode_dollars_hashes_and_backslashes(self) -> None:
        names = ['app.rs', 'folder name/λ.rs', 'hash#cash$.rs', 'back\\slash.rs']
        for name in names[1:]:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('fn original(){}\n')
        raw = 'target: app.rs folder\\ name/λ.rs hash\\#cash$$.rs back\\\\slash.rs \\\n app.rs\n\n# env-dep:SECRET=not-retained\n'
        self.assertEqual(rust_build.dependency_paths(raw, self.root), sorted(self.root / name for name in names))

    def test_empty_malformed_unfinished_and_external_dependency_sets_refuse(self) -> None:
        outside = self.base / 'outside.rs'
        outside.write_text('fn outside(){}\n')
        for raw in ('', 'no dependency target', 'target: ', 'target: app.rs\\', 'target: ../outside.rs'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                rust_build.dependency_paths(raw, self.root)

    def test_inventory_is_typed_closed_ordered_and_bound_to_original_byte_ranges(self) -> None:
        self.assertEqual(self.read_inventory(self.inventory), self.inventory)
        self.assertEqual(rust_build.selected(policy(), self.root, self.inventory), [self.row['identity']])
        for key, changed in (('identity', 'forged-name'), ('file', '/outside.rs'), ('start', True),
                             ('end', 100000), ('expanded', 1), ('compiler_name', '')):
            value = copy.deepcopy(self.inventory)
            value['definitions'][0][key] = changed
            with self.subTest(key=key), self.assertRaises(ValueError): self.read_inventory(value)
        for value in ([], {}, {**self.inventory, 'schema_version': True},
                      {**self.inventory, 'sources': {}}, {**self.inventory, 'definitions': [None]},
                      {**self.inventory, 'definitions': [self.row, self.row]}):
            with self.subTest(value=value), self.assertRaises(ValueError): self.read_inventory(value)

    def test_exclusions_take_precedence_and_unsupported_selected_bodies_refuse(self) -> None:
        excluded = copy.deepcopy(policy().data)
        excluded['functions']['exclude'] = ['*workload*']
        self.assertEqual(rust_build.selected(Policy.parse(excluded), self.root, self.inventory), [])
        excluded['functions']['exclude'] = []
        excluded['sources']['exclude'] = ['app.rs']
        self.assertEqual(rust_build.selected(Policy.parse(excluded), self.root, self.inventory), [])
        for key in ('asynchronous', 'expanded'):
            value = copy.deepcopy(self.inventory)
            value['definitions'][0][key] = True
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'completion profile'):
                rust_build.selected(policy(), self.root, value)
        value = copy.deepcopy(self.inventory)
        value['definitions'][0]['compiler_name'] = 'workload::{closure#0}'
        with self.assertRaisesRegex(ValueError, 'completion profile'):
            rust_build.selected(policy(), self.root, value)

    def test_unimplemented_policy_requests_refuse_before_build_side_effects(self) -> None:
        for section, key, value in (('context', 'tasks', True), ('context', 'http', True)):
            data = copy.deepcopy(policy().data)
            data[section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                rust_build.validate_policy(Policy.parse(data))
        with self.assertRaises(ValueError):
            rust_build.validate_policy(Policy.parse({'schema_version': 3, 'languages': ['java']}))
        for optimisation in (True, -1, 4):
            with self.subTest(optimisation=optimisation), self.assertRaises(ValueError):
                rust_build.build(policy(), self.root, self.source, self.base / 'never-created',
                    tools_directory=self.base, sdk_directory=self.base, optimisation=optimisation)
        self.assertFalse((self.base / 'never-created').exists())

    def test_rehashed_nonobject_compiled_policy_is_a_controlled_refusal(self) -> None:
        for changed in (None, [], [[]], 'not a policy'):
            value = self.literal_build()
            value['policy'] = changed
            value['policy_sha256'] = native_launch.digest(changed)
            with self.subTest(policy=changed), self.assertRaisesRegex(ValueError, 'profile'):
                self.verify_literal(value)

    def test_wrongly_typed_entry_and_changed_selectors_refuse_before_sdk_reads(self) -> None:
        for changed in (None, [], True):
            value = self.literal_build()
            value['entry'] = changed
            with self.subTest(entry=changed), self.assertRaisesRegex(ValueError, 'profile'):
                self.verify_literal(value)
        current = copy.deepcopy(policy().data)
        current['functions']['include'] = ['different-function']
        with self.assertRaisesRegex(ValueError, 'profile'):
            self.verify_literal(self.literal_build(), Policy.parse(current))


class RustBoundaryTests(unittest.TestCase):
    """Generated directories must stay outside the physical original inputs."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'Application'
        self.root.mkdir()
        (self.root / 'app.rs').write_text('fn main() {}\n')
        self.alias = self.alias_for(self.root)
        self.build = self.base / 'Retained'
        self.build.mkdir()
        self.build_alias = self.alias_for(self.build)

    def alias_for(self, path: Path) -> Path:
        alias = path.with_name(path.name.swapcase())
        # On a case-sensitive filesystem exercise the same physical-alias
        # contract using a symlink. macOS exercises the original case alias.
        if not alias.exists():
            alias.symlink_to(path, target_is_directory=True)
        self.assertTrue(alias.samefile(path))
        return alias

    def test_application_outputs_refuse_ordinary_and_physical_source_aliases_before_tool_reads(self) -> None:
        for root in (self.root, self.alias):
            output = root / 'generated'
            with self.subTest(root=root), \
                    mock.patch.object(rust_compiler, 'read', side_effect=AssertionError('tool sentinel')), \
                    self.assertRaisesRegex(ValueError, 'outside the application root'):
                rust_build.build(policy(), self.root, Path('app.rs'), output,
                    tools_directory=self.build, sdk_directory=self.build)
            self.assertFalse(output.exists())

    def test_runtime_refuses_source_and_build_aliases_before_process_or_plan_creation(self) -> None:
        built = rust_build.Build(self.build, '{}')
        for root in (self.root, self.alias, self.build, self.build_alias):
            runtime = root / 'runtime'
            with self.subTest(root=root), \
                    mock.patch.object(rust_build, 'verify', return_value={'root': str(self.root)}), \
                    mock.patch.object(rust_launch.subprocess, 'Popen', side_effect=AssertionError('process sentinel')), \
                    self.assertRaisesRegex(ValueError, 'outside original sources and retained build'):
                rust_launch.start(built, policy(), runtime, plain=True)
            self.assertFalse(runtime.exists())

    def test_tool_outputs_refuse_ordinary_and_physical_aliases_before_tool_input_reads(self) -> None:
        rust = self.base / 'native/rust'
        rust.mkdir(parents=True)
        alias = self.alias_for(rust)
        for root in (rust, alias):
            output = root / 'generated'
            with self.subTest(root=root), mock.patch.object(rust_compiler, 'ROOT', self.base), \
                    mock.patch.object(rust_compiler, 'sources', side_effect=AssertionError('input sentinel')), \
                    self.assertRaisesRegex(ValueError, 'outside its source directory'):
                rust_compiler.build(output)
            self.assertFalse(output.exists())

    def test_unrelated_new_output_and_shared_prefix_remain_external(self) -> None:
        for path in (self.base / 'Application-copy/generated', self.base / 'new/nested/output'):
            with self.subTest(path=path):
                self.assertFalse(rust_compiler.physical_within(path, self.root))
                self.assertFalse(path.exists())

    def test_retained_reader_refuses_source_alias_before_tool_records(self) -> None:
        data = policy().data
        value = {'schema_version': 1, 'kind': 'otelc.rust.source-build',
            'profile': rust_build.PROFILE, 'root': str(self.root), 'entry': str(self.root / 'app.rs'),
            'policy': data, 'policy_sha256': native_launch.digest(data),
            'selection_sha256': native_launch.digest(rust_build.selection(data)),
            'tools': {}, 'sdk': {}, 'inputs': [], 'requests': [], 'selected': [],
            'optimisation': 2, 'inventories': [], 'products': [], 'source_complete': False}
        encoded = json.dumps(value)
        for directory in (self.root, self.alias):
            with self.subTest(directory=directory), \
                    mock.patch.object(rust_build, 'checked_file', return_value=({}, encoded.encode())), \
                    mock.patch.object(rust_compiler, 'read', side_effect=AssertionError('tool sentinel')), \
                    self.assertRaisesRegex(ValueError, 'original root or external output'):
                rust_build.verify(rust_build.Build(directory, encoded), policy())


class RustPublicExecutionTests(unittest.TestCase):
    """Use genuine products; an explicit retained fixture avoids rebuilding locally."""
    @classmethod
    def setUpClass(cls) -> None:
        cls.capture = Capture()
        cls.addClassCleanup(cls.capture.close)
        output = os.environ.get('OTELC_RUST_PUBLIC_TEST_OUTPUT')
        if output:
            cls.base = Path(output).resolve()
            cls.base.mkdir(mode=0o700)
        else:
            temporary = tempfile.TemporaryDirectory(prefix='otelc-public-rust-')
            cls.addClassCleanup(temporary.cleanup)
            cls.base = Path(temporary.name).resolve()
        cls.policy = policy(f'http://127.0.0.1:{cls.capture.server.server_port}')
        selected = os.environ.get('OTELC_RUST_PUBLIC_TEST_BUILD')
        if selected:
            cls.built = rust_build.read(Path(selected), cls.policy)
            cls.root = Path(cls.built.manifest()['root'])
        else:
            if shutil.disk_usage(cls.base).free < rust_compiler.FLOOR:
                raise ValueError('fresh Rust public execution tests require 8 GiB free')
            cls.root = cls.base / 'application'
            cls.root.mkdir()
            (cls.root / 'app.rs').write_text(APPLICATION)
            (cls.root / 'work.rs').write_text(WORK)
            configured_tools = os.environ.get('OTELC_RUST_PUBLIC_TEST_TOOLS')
            tools = rust_compiler.read(Path(configured_tools)) if configured_tools else rust_compiler.build(cls.base / 'tools')
            configured_sdk = os.environ.get('OTELC_RUST_PUBLIC_TEST_SDK')
            sdk = Path(configured_sdk).resolve(strict=True) if configured_sdk else cls.base / 'sdk'
            if not configured_sdk:
                native_launch.build_sdk(sdk)
            cls.built = rust_build.build(cls.policy, cls.root, Path('app.rs'), cls.base / 'compiled',
                tools_directory=tools.directory, sdk_directory=sdk)
        if ((cls.root / 'app.rs').read_text() != APPLICATION or (cls.root / 'work.rs').read_text() != WORK):
            raise ValueError('retained Rust test build is not the declared unedited fixture')
        cls.source_inputs = {str(path): native_gate.identity(path) for path in cls.root.glob('*.rs')}
        cls.applications: list[dict] = []

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.source_inputs != {str(path): native_gate.identity(path) for path in cls.root.glob('*.rs')}:
            raise AssertionError('public Rust application sources changed')
        rust_build.verify(cls.built, cls.policy)
        (cls.base / 'source-inputs.json').write_text(json.dumps(cls.source_inputs, indent=2) + '\n')
        (cls.base / 'applications.json').write_text(json.dumps(cls.applications, indent=2) + '\n')
        for kind in ('traces', 'metrics'):
            for index, message in enumerate(getattr(cls.capture, kind)):
                # Capture stores decoded protobufs, not original HTTP bodies.
                (cls.base / f'decoded-{kind}-{index}.protobuf').write_bytes(message.SerializeToString())

    def setUp(self) -> None:
        self.runtime = self.base / self._testMethodName
        self.children: list[subprocess.Popen] = []
        self.addCleanup(self.clean_children)
        self.environ = mock.patch.dict(os.environ, {'RUST_BACKTRACE': '0',
            'LLVM_PROFILE_FILE': str(self.base / 'native-%m-%p.profraw')})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def clean_children(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
            for stream in (child.stdout, child.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            child.wait(timeout=10)
            self.applications.append({'test': self._testMethodName, 'pid': child.pid,
                                      'exit_code': child.returncode, 'arguments': child.args})

    def line(self, child: subprocess.Popen) -> bytes:
        self.assertTrue(select.select([child.stdout], [], [], 15)[0], 'Rust response timed out')
        return child.stdout.readline()

    def lifetime_build(self) -> tuple[rust_build.Build, Policy]:
        """Reuse actual SDK/tools, never an authored compiled build fixture."""
        cls = type(self)
        if not hasattr(cls, 'lifetime_built'):
            if shutil.disk_usage(self.base).free < rust_compiler.FLOOR:
                raise ValueError('fresh Rust lifetime applications require 8 GiB free')
            root = self.base / 'lifetime-application'; root.mkdir()
            entry = root / 'LifetimeApp.rs'
            entry.write_bytes((rust_build.ROOT / 'examples/rust/LifetimeApp.rs').read_bytes())
            data = Policy.load(rust_build.ROOT / 'examples/rust-lifetimes.toml').data
            data['export'].update(self.policy.data['export'])
            cls.lifetime_current = Policy.parse(data)
            original = self.built.manifest()
            cls.lifetime_built = rust_build.build(cls.lifetime_current, root, Path(entry.name),
                self.base / 'lifetime-build', tools_directory=Path(original['tools']['path']).parent,
                sdk_directory=Path(original['sdk']['path']).parent)
        return cls.lifetime_built, cls.lifetime_current

    def lifetime_panic_output(self, raw: bytes) -> bytes:
        """Only the compiler's documented per-thread numeric label differs."""
        normalized, count = re.subn(rb"(?m)^(thread 'main' )\([0-9]+\)( panicked at )",
                                    rb'\1(<thread-id>)\2', raw)
        self.assertEqual(count, 2)
        return normalized

    def exercise_public_lifetimes(self, built: rust_build.Build, current: Policy,
                                  expected_error: bytes) -> None:
        runtime = self.runtime.with_name(current.data['resource']['service_name'])
        child = rust_launch.start(built, current, runtime, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.children.append(child)
        self.assertEqual(self.line(child), b'ready\n')
        endpoint = runtime / 'control.json'; control.wait_ready(endpoint, child.pid)

        def call(value: int, expected: bytes) -> None:
            child.stdin.write(f'{value}\n'.encode()); child.stdin.flush()
            self.assertEqual(self.line(child), expected)

        call(1, b'7 3\n')
        control.request(endpoint, 'collection on'); control.request(endpoint, 'emission off')
        call(2, b'-73 6\n')
        exports = control.request(endpoint, 'health')['metric_exports']['batches_started']
        control.request(endpoint, 'flush')
        self.assertEqual(control.request(endpoint, 'health')['metric_exports']['batches_started'], exports)
        control.request(endpoint, 'collection off'); control.request(endpoint, 'emission on')
        call(3, b'-73 9\n')
        control.request(endpoint, 'on'); call(1, b'7 12\n')
        self.assertEqual(control.request(endpoint, 'status')['pid'], child.pid)
        stdout, stderr = child.communicate(timeout=15)
        self.assertEqual((child.returncode, stdout), (0, b''))
        self.assertEqual(self.lifetime_panic_output(stderr), expected_error)
        final = health.read(runtime / 'health.json')
        rust_launch.validate_health(final, built, current, child.pid)
        native_benchmark.validate_drain(final, current)
        self.assertEqual(final['drop_accounting'], dict(types='2', admitted='12', completed='12',
            live='0', dropping='0', ending='0', censored='0', lost='0', duplicates='0', invalid='0',
            closing=True, closed=True))
        plan = json.loads((runtime / 'plan.json').read_bytes())
        self.assertEqual(plan['schema_version'], 3)
        del plan['token']
        (runtime / 'rust-plan-profile.json').write_text(json.dumps(plan, indent=2) + '\n')
        self.check_public_lifetime_spans(current.data['resource']['service_name'])
        calls = [point.as_int for request in self.capture.metrics for resource in request.resource_metrics
                 if any(item.key == 'service.name' and item.value.string_value == current.data['resource']['service_name']
                        for item in resource.resource.attributes)
                 for scope in resource.scope_metrics for metric in scope.metrics
                 if metric.name == 'otelc.function.calls' for point in metric.sum.data_points]
        self.assertEqual(max(calls), 3 if current.data['metrics']['collection'] else 2)

    def public_lifetime_spans(self, service: str) -> list:
        return [span for request in self.capture.traces for resource in request.resource_spans
                if any(item.key == 'service.name' and item.value.string_value == service
                       for item in resource.resource.attributes)
                for scope in resource.scope_spans for span in scope.spans]

    def check_public_lifetime_spans(self, service: str) -> None:
        spans = self.public_lifetime_spans(service)
        functions = {span.span_id for span in spans if span.name == 'LifetimeApp.rs::workload'}
        lifetimes = [span for span in spans if span.name.endswith(' lifetime')]
        self.assertEqual((len(spans), len(functions), len(lifetimes)), (16, 4, 12))
        exceptional = []
        for span in lifetimes:
            self.assertFalse(span.parent_span_id)
            self.assertEqual(len(span.links), 1)
            self.assertIn(span.links[0].span_id, functions)
            labels = {item.key: item.value for item in span.attributes}
            self.assertEqual(labels['boundary'].string_value, 'drop_entry')
            exceptional.append(labels['otelc.drop.exception_exit'].bool_value)
        self.assertEqual(sorted(exceptional), [False] * 11 + [True])

    def test_public_lifetime_build_replay_all_live_states_and_unwind_preserve_originals(self) -> None:
        built, current = self.lifetime_build()
        metadata = built.manifest()
        self.assertEqual((metadata['schema_version'], metadata['profile']), (2, rust_build.LIFETIME_PROFILE))
        source = Path(metadata['entry']); original = native_gate.identity(source)
        stock = rust_launch.start(built, current, self.runtime, plain=True, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.children.append(stock)
        stdout, stderr = stock.communicate(input=b'1\n2\n3\n1\n', timeout=15)
        self.assertEqual((stock.returncode, stdout), (0, b'ready\n7 3\n-73 6\n-73 9\n7 12\n'))
        expected_error = self.lifetime_panic_output(stderr)
        for collection, emission in ((False, False), (False, True), (True, False), (True, True)):
            data = copy.deepcopy(current.data)
            data['resource']['service_name'] = f'public-lifetimes-{int(collection)}{int(emission)}'
            data['metrics'].update(collection=collection, emission=emission)
            selected = Policy.parse(data)
            with mock.patch.object(rust_compiler, 'execute', side_effect=AssertionError('replay must not compile')):
                retained = rust_build.read(built.directory, selected)
                self.exercise_public_lifetimes(retained, selected, expected_error)
        self.assertEqual(native_gate.identity(source), original)
        self.assertEqual(source.read_bytes(), (rust_build.ROOT / 'examples/rust/LifetimeApp.rs').read_bytes())

    def test_public_lifetime_selection_and_unsupported_foreign_types_refuse(self) -> None:
        built, current = self.lifetime_build()
        for key, changed in (('enabled', False), ('include', ['*Zero']), ('exclude', ['*Item*'])):
            data = copy.deepcopy(current.data); data['lifetimes'][key] = changed
            with self.subTest(key=key), self.assertRaises(ValueError):
                rust_build.read(built.directory, Policy.parse(data))
        root = self.base / 'lifetime-foreign'; root.mkdir()
        source = root / 'app.rs'
        source.write_text('fn workload(){let file=std::fs::File::open("never-executed").unwrap();'
                          'std::hint::black_box(&file);}fn main(){workload();}')
        original = native_gate.identity(source)
        data = copy.deepcopy(current.data); data['lifetimes']['include'] = ['*File*']
        selected = Policy.parse(data); metadata = built.manifest()
        output = self.base / 'lifetime-foreign-build'
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            rust_build.build(selected, root, Path('app.rs'), output,
                tools_directory=Path(metadata['tools']['path']).parent,
                sdk_directory=Path(metadata['sdk']['path']).parent)
        self.assertFalse((output / 'instrumented').exists())
        self.assertFalse((output / rust_build.MANIFEST).exists())
        self.assertEqual(native_gate.identity(source), original)

    def test_public_lifetime_cli_uses_the_same_external_policy_in_both_lanes(self) -> None:
        built, current = self.lifetime_build()
        config = self.base / 'public-rust-lifetimes.toml'
        config.write_text((rust_build.ROOT / 'examples/rust-lifetimes.toml').read_text() +
            '\n[export]\nendpoint = ' + json.dumps(current.data['export']['endpoint']) + '\n')
        selected = Policy.load(config)
        for plain in (True, False):
            runtime = self.runtime.with_name(self.runtime.name + ('-plain' if plain else '-sdk'))
            command = [str(rust_build.ROOT / 'bin/quux-otelc'), '--config', str(config),
                'rust-run', '--build', str(built.directory), '--runtime', str(runtime)]
            if plain:
                command.append('--plain')
            child = subprocess.Popen(command, cwd=rust_build.ROOT,
                env=os.environ | {'OTELC_PYTHON': sys.executable}, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.children.append(child)
            stdout, stderr = child.communicate(input=b'1\n', timeout=20)
            self.assertEqual((child.returncode, stdout, stderr), (0, b'ready\n7 3\n', b''))
            execution = json.loads((runtime / 'rust-execution.json').read_bytes())
            self.assertEqual(execution['profile'], rust_build.LIFETIME_PROFILE)
            self.assertIsNone(execution['validation_failure'])
            if not plain:
                rust_launch.validate_health(execution['sdk_health'], built, selected, execution['pid'])
                self.assertEqual(execution['sdk_health']['drop_accounting']['completed'], '3')

    def test_public_selected_by_value_ownership_refuses_before_publishing_a_build(self) -> None:
        built, current = self.lifetime_build()
        metadata = built.manifest()
        root = self.base / 'lifetime-argument'; root.mkdir()
        source = root / 'app.rs'
        source.write_text('struct Item;impl Drop for Item{fn drop(&mut self){}}'
            'fn consume(item:Item){std::hint::black_box(&item);}fn main(){consume(Item);}')
        original = native_gate.identity(source)
        output = self.base / 'lifetime-argument-build'
        with self.assertRaises(ValueError):
            rust_build.build(current, root, Path('app.rs'), output,
                tools_directory=Path(metadata['tools']['path']).parent,
                sdk_directory=Path(metadata['sdk']['path']).parent)
        self.assertFalse((output / 'instrumented').exists())
        self.assertFalse((output / rust_build.MANIFEST).exists())
        self.assertEqual(native_gate.identity(source), original)

    def test_public_lifetime_capacity_and_trace_policy_reach_the_running_sdk(self) -> None:
        built, current = self.lifetime_build()
        for limit, traces in ((1, True), (4096, False)):
            data = copy.deepcopy(current.data)
            data['lifetimes']['max_live'] = limit
            data['traces']['enabled'] = traces
            data['resource']['service_name'] = f'rust-drop-limit-{limit}'
            selected = Policy.parse(data)
            runtime = self.runtime.with_name(data['resource']['service_name'])
            retained = rust_build.read(built.directory, selected)
            child = rust_launch.start(retained, selected, runtime, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self.children.append(child)
            stdout, stderr = child.communicate(input=b'1\n', timeout=15)
            self.assertEqual((child.returncode, stdout, stderr), (0, b'ready\n7 3\n', b''))
            final = health.read(runtime / 'health.json')
            rust_launch.validate_health(final, retained, selected, child.pid)
            expected = ('1', '2') if limit == 1 else ('3', '0')
            self.assertEqual((final['drop_accounting']['admitted'], final['drop_accounting']['lost']), expected)
            self.assertEqual(final['drop_accounting']['completed'], expected[0])
            self.assertEqual(final['function_accounting']['ended'], '1')
            self.assertEqual(len(self.public_lifetime_spans(data['resource']['service_name'])), 2 if traces else 0)
            if limit == 1:
                with self.assertRaisesRegex(ValueError, 'observation loss'):
                    native_benchmark.validate_drain(final, selected)
            else:
                native_benchmark.validate_drain(final, selected)
                self.assertEqual(final['span_accounting']['sampled_started'], '0')
                self.assertEqual(final['trace_exports']['units_started'], '0')

    def test_shared_lifetime_benchmark_policy_retains_real_drop_drain(self) -> None:
        original = self.built.manifest()
        root = self.base / 'lifetime-benchmark-application'
        source = root / rust_benchmark.EXAMPLE; source.parent.mkdir(parents=True)
        source.write_bytes((rust_benchmark.ROOT / rust_benchmark.EXAMPLE).read_bytes())
        data = Policy.load(rust_build.ROOT / 'examples/otelc.toml').data
        data['export'].update(self.policy.data['export'])
        data['benchmark'].update(iterations=3, samples=2, warmup=1)
        data['traces'].update(sample_ratio=1.0)
        data['resource']['service_name'] = 'rust-drop-benchmark'
        current = Policy.parse(data)
        built = rust_build.build(current, root, rust_benchmark.EXAMPLE, self.base / 'lifetime-benchmark-build',
            tools_directory=Path(original['tools']['path']).parent,
            sdk_directory=Path(original['sdk']['path']).parent)
        output = self.base / 'rust-drop-benchmark'
        report = rust_benchmark.run(current, output, build_directory=built.directory,
            comparison_id='actual-rust-drop-smoke', publish=False)
        self.assertEqual(report['launch_profile'], rust_build.LIFETIME_PROFILE)
        self.assertEqual(report['expected_checksum'], rust_benchmark.benchmark.checksum(3))
        self.assertEqual(set(report['samples_ns_per_operation']), {'plain', *rust_benchmark.benchmark.STATES})
        self.assertEqual({row['pid'] for row in report['transitions']}, {report['pids']['instrumented']})
        self.assertEqual(report['sdk_health']['drop_accounting']['admitted'], '28')
        self.assertEqual(report['sdk_health']['drop_accounting']['completed'], '28')
        rust_build.verify(built, current)

    def test_literal_arguments_and_runtime_environment_preserve_original_output(self) -> None:
        arguments = ('--option=two words', 'λ', '')
        for plain in (True, False):
            runtime = self.runtime.with_name(self.runtime.name + ('-plain' if plain else '-sdk'))
            child = rust_launch.start(self.built, self.policy, runtime, plain=plain, arguments=arguments,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self.children.append(child)
            self.assertEqual(child.args[1:], list(arguments))
            out, err = child.communicate(input=b'3\n', timeout=15)
            self.assertEqual((child.returncode, out, err), (0, b'ready\n12 1\n', b''))
            self.assertEqual((runtime / 'plan.json').exists(), not plain)
            if not plain:
                rust_launch.validate_health(health.read(runtime / 'health.json'), self.built, self.policy, child.pid)

    def test_all_startup_states_live_controls_and_panics_preserve_the_original_program(self) -> None:
        stock = rust_launch.start(self.built, self.policy, self.runtime, plain=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.children.append(stock)
        stdout, original_error = stock.communicate(input=b'3\n2\n-1\n4\n-1\n', timeout=15)
        self.assertEqual((stock.returncode, stdout), (0, b'ready\n12 1\n5 2\n-73 3\n22 4\n-73 5\n'))
        def panic_output(raw: bytes) -> bytes:
            normalized, count = re.subn(rb"(?m)^(thread 'main' )\([0-9]+\)( panicked at )",
                                        rb'\1(<thread-id>)\2', raw)
            self.assertEqual(count, 2)
            return normalized
        original_error = panic_output(original_error)
        for collection, emission in ((False, False), (False, True), (True, False), (True, True)):
            name = f'public-rust-{int(collection)}{int(emission)}'
            runtime = self.runtime.with_name(name)
            data = copy.deepcopy(self.policy.data)
            data['resource']['service_name'] = name
            data['metrics'].update(collection=collection, emission=emission)
            current = Policy.parse(data)
            child = rust_launch.start(self.built, current, runtime, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self.children.append(child)
            self.assertEqual(self.line(child), b'ready\n')
            endpoint = runtime / 'control.json'
            control.wait_ready(endpoint, child.pid)
            def call(value: int, expected: bytes) -> None:
                child.stdin.write(f'{value}\n'.encode())
                child.stdin.flush()
                self.assertEqual(self.line(child), expected)
            call(3, b'12 1\n')
            control.request(endpoint, 'flush')
            control.request(endpoint, 'collection on')
            control.request(endpoint, 'emission off')
            call(2, b'5 2\n')
            exports = control.request(endpoint, 'health')['metric_exports']['batches_started']
            control.request(endpoint, 'flush')
            self.assertEqual(control.request(endpoint, 'health')['metric_exports']['batches_started'], exports)
            control.request(endpoint, 'emission on')
            control.request(endpoint, 'flush')
            control.request(endpoint, 'collection off')
            call(-1, b'-73 3\n')
            control.request(endpoint, 'collection on')
            call(4, b'22 4\n')
            call(-1, b'-73 5\n')
            self.assertEqual(control.request(endpoint, 'status')['pid'], child.pid)
            out, err = child.communicate(timeout=15)
            self.assertEqual((child.returncode, out, panic_output(err)), (0, b'', original_error))
            report = health.read(runtime / 'health.json')
            rust_launch.validate_health(report, self.built, current, child.pid)
            self.assertEqual({key: report['function_accounting'][key] for key in
                ('registered', 'started', 'ended', 'active', 'lost', 'invalid')},
                dict(registered='1', started='5', ended='5', active='0', lost='0', invalid='0'))
            spans = [span for request in self.capture.traces for resource in request.resource_spans
                     if any(row.key == 'service.name' and row.value.string_value == name
                            for row in resource.resource.attributes)
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), 5)
            self.assertTrue(all(span.name == 'work.rs::work::workload' for span in spans))
            self.assertEqual(sum(span.status.code == 2 for span in spans), 2)
            points = {metric.name: sum(point.as_int for point in metric.sum.data_points)
                for request in self.capture.metrics for resource in request.resource_metrics
                if any(row.key == 'service.name' and row.value.string_value == name
                       for row in resource.resource.attributes)
                for scope in resource.scope_metrics for metric in scope.metrics
                if metric.name in ('otelc.function.calls', 'otelc.function.errors')}
            self.assertEqual(points, {'otelc.function.calls': 4 if collection else 3,
                                      'otelc.function.errors': 1})

    def test_bad_lane_arguments_and_overlapping_runtime_refuse_before_spawn(self) -> None:
        for options in ({'plain': 1}, {'arguments': []}, {'arguments': ('nul\0value',)},
                        {'arguments': ('x' * (1024 * 1024 + 1),)}):
            with self.subTest(options=list(options)), self.assertRaises(ValueError):
                rust_launch.start(self.built, self.policy, self.runtime, **options)
        self.assertFalse(self.runtime.exists())
        for runtime in (self.root / 'runtime', self.built.directory / 'runtime'):
            with self.subTest(runtime=runtime), self.assertRaises(ValueError):
                rust_launch.start(self.built, self.policy, runtime)

    def test_direct_run_records_real_success_for_both_lanes(self) -> None:
        real_start = rust_launch.start
        def spawn(*args, **kwargs):
            child = real_start(*args, **kwargs, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, bufsize=0)
            self.children.append(child)
            child.stdin.write(b'3\n')
            child.stdin.close()
            return child
        for plain in (True, False):
            runtime = self.runtime.with_name(self.runtime.name + ('-plain' if plain else '-sdk'))
            with mock.patch.object(rust_launch, 'start', side_effect=spawn):
                code = rust_launch.run(self.policy, runtime, build_directory=self.built.directory, plain=plain)
            child = self.children[-1]
            self.assertEqual((code, child.stdout.read(), child.stderr.read()), (0, b'ready\n12 1\n', b''))
            report = json.loads((runtime / 'rust-execution.json').read_bytes())
            self.assertEqual((report['pid'], report['process_exit_code'], report['validation_failure']),
                             (child.pid, 0, None))
            self.assertEqual(report['sdk_health'] is None, plain)

    def test_isolated_cli_uses_common_policy_and_preserves_application_streams(self) -> None:
        config = self.base / 'common-functions.toml'
        # Keep the shared selectors/resource intact; only route exports to this
        # test's owned receiver instead of requiring the demonstration stack.
        config.write_text((rust_build.ROOT / 'examples/functions.toml').read_text() +
            '\n[export]\nendpoint = ' + json.dumps(self.policy.data['export']['endpoint']) + '\n')
        current = Policy.load(config)
        for plain in (True, False):
            runtime = self.runtime.with_name(self.runtime.name + ('-plain' if plain else '-sdk'))
            command = [str(rust_build.ROOT / 'bin/quux-otelc'), '--config', str(config),
                       'rust-run', '--build', str(self.built.directory), '--runtime', str(runtime)]
            if plain:
                command.append('--plain')
            command += ['--', '--literal=two words', 'λ', '']
            child = subprocess.Popen(command, cwd=rust_build.ROOT,
                env={**os.environ, 'OTELC_PYTHON': sys.executable},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.children.append(child)
            out, err = child.communicate(input=b'3\n', timeout=20)
            self.assertEqual((child.returncode, out, err), (0, b'ready\n12 1\n', b''))
            report = json.loads((runtime / 'rust-execution.json').read_bytes())
            self.assertEqual((report['process_exit_code'], report['launcher_exit_code'],
                              report['validation_failure']), (0, 0, None))
            self.assertNotEqual(report['pid'], child.pid)
            self.assertEqual(report['sdk_health'] is None, plain)
            if not plain:
                rust_launch.validate_health(report['sdk_health'], self.built, current, report['pid'])
                self.assertEqual(report['sdk_health']['function_accounting']['ended'], '1')

    def test_signal_exit_retains_the_actual_code_and_missing_final_sdk_health(self) -> None:
        real_start = rust_launch.start
        def spawn(*args, **kwargs):
            child = real_start(*args, **kwargs, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, bufsize=0)
            self.children.append(child)
            self.assertEqual(self.line(child), b'ready\n')
            control.wait_ready(self.runtime / 'control.json', child.pid)
            child.terminate()
            return child
        with mock.patch.object(rust_launch, 'start', side_effect=spawn):
            code = rust_launch.run(self.policy, self.runtime, build_directory=self.built.directory)
        self.assertEqual(code, 128 + signal.SIGTERM)
        report = json.loads((self.runtime / 'rust-execution.json').read_bytes())
        self.assertEqual(report['process_exit_code'], -signal.SIGTERM)
        self.assertIsNone(report['sdk_health'])
        self.assertIsNotNone(report['validation_failure'])

    def test_interrupted_wait_reaps_the_actual_owned_child(self) -> None:
        for stopped in (False, True):
            runtime = self.runtime.with_name(self.runtime.name + ('-stopped' if stopped else '-running'))
            child = rust_launch.start(self.built, self.policy, runtime,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self.children.append(child)
            self.assertEqual(self.line(child), b'ready\n')
            control.wait_ready(runtime / 'control.json', child.pid)
            if stopped:
                child.send_signal(signal.SIGSTOP)
            real_wait = child.wait
            def wait(timeout=None):
                if timeout is None:
                    raise KeyboardInterrupt
                return real_wait(timeout=timeout)
            with mock.patch.object(rust_launch, 'start', return_value=child), \
                    mock.patch.object(child, 'wait', side_effect=wait), \
                    (mock.patch.object(child, 'terminate', return_value=None) if stopped else nullcontext()):
                # Fault injection leaves the real stopped child alive after the
                # first termination request, so the bounded kill fallback runs.
                with self.assertRaises(KeyboardInterrupt):
                    rust_launch.run(self.policy, runtime, build_directory=self.built.directory)
            self.assertIsNotNone(child.poll(), 'an interrupted launcher left its application running')
            if stopped:
                self.assertEqual(child.returncode, -signal.SIGKILL)

    def test_zero_exit_without_sdk_checkpoint_is_not_successful_instrumentation(self) -> None:
        real_start = rust_launch.start
        def spawn(*args, **kwargs):
            # Deliberate negative control: a genuine stock image returns correct
            # results but cannot supply SDK health for the requested woven lane.
            kwargs['plain'] = True
            child = real_start(*args, **kwargs, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, bufsize=0)
            self.children.append(child)
            child.stdin.write(b'3\n')
            child.stdin.close()
            return child
        with mock.patch.object(rust_launch, 'start', side_effect=spawn):
            code = rust_launch.run(self.policy, self.runtime, build_directory=self.built.directory)
        self.assertEqual(code, 1)
        report = json.loads((self.runtime / 'rust-execution.json').read_bytes())
        self.assertEqual(report['process_exit_code'], 0)
        self.assertIsNone(report['sdk_health'])
        self.assertIsNotNone(report['validation_failure'])

    def test_final_health_is_bound_to_the_actual_pid_policy_selection_and_language(self) -> None:
        child = rust_launch.start(self.built, self.policy, self.runtime,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.children.append(child)
        child.communicate(input=b'3\n', timeout=15)
        original = health.read(self.runtime / 'health.json')
        for key, value in (('pid', child.pid + 1), ('phase', 'running'), ('language', 'cpp'), ('sdk_attached', False),
                           ('policy_sha256', '0' * 64), ('selection_sha256', '0' * 64)):
            changed = copy.deepcopy(original)
            changed[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'does not identify'):
                rust_launch.validate_health(changed, self.built, self.policy, child.pid)

    def test_ordinary_benchmark_example_all_startup_states_and_public_cli(self) -> None:
        """Build the maintained example with real tools and SDK, never a protocol double."""
        original = native_gate.identity(rust_benchmark.ROOT / rust_benchmark.EXAMPLE)
        root = self.base / 'benchmark-application'
        source = root / rust_benchmark.EXAMPLE
        source.parent.mkdir(parents=True)
        source.write_bytes((rust_benchmark.ROOT / rust_benchmark.EXAMPLE).read_bytes())
        data = Policy.load(rust_benchmark.ROOT / 'examples/benchmark-metrics.toml').data
        data['export'].update(endpoint=f'http://127.0.0.1:{self.capture.server.server_port}',
                              interval_ms=60000, shutdown_timeout_ms=5000)
        data['traces'].update(sample_ratio=1.)
        data['benchmark'].update(iterations=3, warmup=1, samples=2)
        selected = Policy.parse(data)
        metadata = self.built.manifest()
        built = rust_build.build(selected, root, rust_benchmark.EXAMPLE, self.base / 'benchmark-compiled',
            tools_directory=Path(metadata['tools']['path']).parent,
            sdk_directory=Path(metadata['sdk']['path']).parent)
        for collection, emission in ((False, False), (False, True), (True, False), (True, True)):
            data['metrics'].update(collection=collection, emission=emission)
            service = f'rust-benchmark-{int(collection)}{int(emission)}'
            data['resource']['service_name'] = service
            current = Policy.parse(data)
            output = self.base / service
            report = rust_benchmark.run(current, output, build_directory=built.directory,
                                       comparison_id='actual-rust-example', publish=False)
            self.assertEqual(report['expected_checksum'], rust_benchmark.benchmark.checksum(3))
            self.assertEqual(set(report['samples_ns_per_operation']), {'plain', *rust_benchmark.benchmark.STATES})
            self.assertTrue(all(len(values) == 2 for values in report['samples_ns_per_operation'].values()))
            rows = report['process_samples']
            self.assertEqual(len(rows), 10)
            self.assertTrue(all(row['checksum'] == report['expected_checksum'] for row in rows))
            self.assertTrue(all(set(row['process_usage']) == {'application'} for row in rows))
            for row in rows:
                usage = row['process_usage']['application']
                self.assertEqual(row['pid'], report['pids']['plain' if row['mode'] == 'plain' else 'instrumented'])
                self.assertEqual(usage['before']['pid'], row['pid'])
                self.assertEqual(usage['after']['pid'], row['pid'])
                self.assertGreater(usage['rss_bytes'], 0)
                self.assertGreaterEqual(usage['cpu_ns'], 0)
            self.assertIsNone(report['sdk_pid'])
            self.assertEqual(len(report['transitions']), 8)
            self.assertEqual({row['pid'] for row in report['transitions']}, {report['pids']['instrumented']})
            self.assertEqual(report['sdk_health']['function_accounting']['started'], '28')
            self.assertEqual(report['sdk_health']['span_accounting']['sampled_ended'], '28')
            self.assertFalse((output / 'plain/health.json').exists())
            self.assertFalse((output / 'plain/plan.json').exists())
            self.assertTrue((output / 'metric-finalization.json').is_file())
            for lane in ('plain', 'instrumented'):
                self.assertGreater(report['launch_to_first_response'][lane]['elapsed_ns'], 0)
                for name, expected in report['launch_evidence'][lane].items():
                    self.assertEqual(native_gate.identity(output / lane / name), expected)
            spans = [span for request in self.capture.traces for resource in request.resource_spans
                     if any(attr.key == 'service.name' and attr.value.string_value == service
                            for attr in resource.resource.attributes)
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), 28)
            self.assertTrue(all('workload' in span.name and 'excluded' not in span.name for span in spans))
        configuration = self.base / 'rust-benchmark-cli.toml'
        configuration.write_text((rust_benchmark.ROOT / 'examples/benchmark-metrics.toml').read_text()
            .replace('http://127.0.0.1:4320', data['export']['endpoint'])
            .replace('iterations = 10000', 'iterations = 3')
            .replace('warmup = 5', 'warmup = 1').replace('samples = 15', 'samples = 2'))
        command = [str(rust_benchmark.ROOT / 'bin/quux-otelc'), '--config', str(configuration),
            'benchmark', '--language', 'rust', '--build', str(built.directory),
            '--output', str(self.base / 'rust-benchmark-cli'), '--comparison-id', 'actual-rust-cli']
        result = subprocess.run(command, env=os.environ | {'OTELC_PYTHON': sys.executable},
            cwd=rust_benchmark.ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        cli_report = json.loads(Path(json.loads(result.stdout)['report']).read_text())
        self.assertEqual(cli_report['comparison_id'], 'actual-rust-cli')
        self.assertTrue(self.capture.points('otelc.benchmark.operation.duration'))
        rust_build.verify(built, selected)
        self.assertEqual(native_gate.identity(rust_benchmark.ROOT / rust_benchmark.EXAMPLE), original)
