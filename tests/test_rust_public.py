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
from otelc import control, health, native_build, native_gate, native_launch
from otelc import rust_build, rust_compiler, rust_launch
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
        for section, key, value in (('lifetimes', 'enabled', True), ('context', 'tasks', True),
                                    ('context', 'http', True)):
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
