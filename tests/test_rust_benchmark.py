"""Bounded Rust benchmark contracts; genuine compiler runs live in test_rust_public."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from otelc import benchmark, native_gate, native_launch, rust_benchmark as adapter, rust_build
from otelc.__main__ import main, run_benchmark
from otelc.config import Policy
from test_native_benchmark import drain


def policy() -> Policy:
    """Use the shared policy with a small test budget, not synthetic Rust timings."""
    data = Policy.load(adapter.ROOT / 'examples/benchmark-metrics.toml').data
    data['benchmark'].update(iterations=2, warmup=1, samples=2)
    return Policy.parse(data)


class RustBenchmarkContractTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'application'
        source = self.root / adapter.EXAMPLE
        source.parent.mkdir(parents=True)
        source.write_bytes((adapter.ROOT / adapter.EXAMPLE).read_bytes())
        self.build = self.base / 'compiled'
        self.build.mkdir()
        (self.build / rust_build.MANIFEST).write_text('literal test record; not a compiler product')
        self.current = policy()
        self.metadata = {'root': str(self.root), 'entry': str(source), 'inputs': [native_gate.identity(source)],
            'selected': ['examples/rust/BenchmarkApp.rs::workload'], 'selection_sha256': '1'*64,
            'tools': {'path': str(self.base / 'tools/manifest.json')}, 'optimisation': 2}
        self.built = Mock(directory=self.build)
        self.built.manifest.side_effect = lambda: copy.deepcopy(self.metadata)
        self.output = self.base / 'results'

    def report(self) -> dict:
        value = drain()
        value.pop('object_accounting')
        value.update(language='rust', policy_sha256=native_launch.digest(self.current.data))
        return value

    def test_final_health_requires_actual_pid_policy_selection_and_clean_drain(self) -> None:
        process = adapter.Application(SimpleNamespace(pid=101), 1)
        value = self.report()
        with patch.object(adapter.health, 'read', return_value=value):
            adapter.final_health(process, self.base, self.built, self.current)
        self.assertIs(process.sdk_report, value)
        mutations = [(None, 'pid', 102), (None, 'language', 'cpp'),
            (None, 'policy_sha256', '2'*64), (None, 'selection_sha256', '2'*64),
            ('function_accounting', 'started', '0'), ('function_accounting', 'lost', '1'),
            ('lifecycle', 'shutdown', 'timed_out'), ('span_accounting', 'queue_pending', '1'),
            ('trace_exports', 'batches_failed', '1'), ('metric_exports', 'units_succeeded', None)]
        for section, key, changed in mutations:
            wrong = copy.deepcopy(value)
            (wrong if section is None else wrong[section])[key] = changed
            process.sdk_report = None
            with self.subTest(section=section, key=key), patch.object(adapter.health, 'read', return_value=wrong), self.assertRaises(ValueError):
                adapter.final_health(process, self.base, self.built, self.current)
            self.assertIsNone(process.sdk_report)

    def test_empty_observation_is_not_a_successful_benchmark(self) -> None:
        value = self.report()
        value['function_accounting'].update(started='0', ended='0')
        process = adapter.Application(SimpleNamespace(pid=101), 1)
        with patch.object(adapter.health, 'read', return_value=value), self.assertRaisesRegex(ValueError, 'no selected'):
            adapter.final_health(process, self.base, self.built, self.current)

    def test_public_launch_and_benchmark_refuse_private_drop_profiles(self) -> None:
        from otelc import native_benchmark, rust_launch

        public = self.report()
        rust_launch.validate_health(public, self.built, self.current, 101)
        native_benchmark.validate_drain(public, self.current)
        for changes in ({}, {'lost': '1'}, {'completed': '0', 'censored': '1'}):
            value = copy.deepcopy(public)
            drops = drain()['object_accounting']
            drops['dropping'] = drops.pop('destroying')
            value.update(schema_version=5, drop_accounting=drops | changes)
            # This remains a valid private health shape, never a public plan2 profile.
            self.assertIs(adapter.health.validate(value), value)
            with self.subTest(changes=changes, boundary='launch'), self.assertRaises(ValueError):
                rust_launch.validate_health(value, self.built, self.current, 101)
            with self.subTest(changes=changes, boundary='benchmark'), self.assertRaises(ValueError):
                native_benchmark.validate_drain(value, self.current)

    def test_public_function_profile_refuses_wrong_versions_and_lifetime_policies(self) -> None:
        from otelc import native_benchmark, rust_launch

        for version in (True, 0, 2, 5):
            value = self.report() | {'schema_version': version}
            with self.subTest(version=version, boundary='launch'), self.assertRaises(ValueError):
                rust_launch.validate_health(value, self.built, self.current, 101)
            with self.subTest(version=version, boundary='benchmark'), self.assertRaises(ValueError):
                native_benchmark.validate_drain(value, self.current)
        selected = copy.deepcopy(self.current.data)
        selected['lifetimes']['enabled'] = True
        lifetimes = Policy.parse(selected)
        value = self.report() | {'policy_sha256': native_launch.digest(lifetimes.data)}
        with self.assertRaises(ValueError):
            rust_launch.validate_health(value, self.built, lifetimes, 101)
        with self.assertRaises(ValueError):
            native_benchmark.validate_drain(value, lifetimes)

    def test_arbitrary_or_changed_example_and_empty_selection_refuse_before_output(self) -> None:
        for key, changed in (('entry', str(self.root / 'other.rs')), ('selected', []), ('inputs', []),
                ('inputs', [{**self.metadata['inputs'][0], 'sha256': '0'*64}]),
                ('inputs', self.metadata['inputs'] * 2)):
            original = self.metadata[key]
            self.metadata[key] = changed
            with self.subTest(key=key), patch.object(adapter.rust_build, 'read', return_value=self.built), self.assertRaisesRegex(ValueError, 'unchanged shared'):
                adapter.run(self.current, self.output, build_directory=self.build)
            self.assertFalse(self.output.exists())
            self.metadata[key] = original

    def test_output_inside_source_or_build_refuses_before_creation(self) -> None:
        for output in (self.root / 'generated', self.build / 'generated'):
            with self.subTest(output=output), patch.object(adapter.rust_build, 'read', return_value=self.built), self.assertRaisesRegex(ValueError, 'outside original'):
                adapter.run(self.current, output, build_directory=self.build)
            self.assertFalse(output.exists())

    def test_physical_alias_refuses_even_when_textual_path_is_external(self) -> None:
        alias = self.root.with_name(self.root.name.upper())
        if not alias.exists() or not alias.samefile(self.root):
            self.skipTest('filesystem has no case alias for this actual directory')
        output = alias / 'generated'
        with patch.object(adapter.rust_build, 'read', return_value=self.built), self.assertRaisesRegex(ValueError, 'outside original'):
            adapter.run(self.current, output, build_directory=self.build)
        self.assertFalse(output.exists())

    def test_existing_output_is_preserved_and_never_reused(self) -> None:
        self.output.mkdir()
        sentinel = self.output / 'existing'
        sentinel.write_text('keep')
        with patch.object(adapter.rust_build, 'read', return_value=self.built), self.assertRaises(FileExistsError):
            adapter.run(self.current, self.output, build_directory=self.build)
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_keyboard_interrupt_remains_primary_while_every_finalizer_runs(self) -> None:
        runtime = self.base / 'instrumented'
        runtime.mkdir()
        owner = Mock(pid=101)
        primary = KeyboardInterrupt('measurement interrupted')
        with patch.object(adapter.rust_launch, 'start', return_value=owner), \
                patch.object(adapter.benchmark, 'stop', side_effect=OSError('stop failed')) as stop, \
                patch.object(adapter.rust_build, 'verify', side_effect=ValueError('inputs changed')) as verify, \
                patch.object(adapter, 'final_health', side_effect=ValueError('SDK failed')) as final:
            with self.assertRaises(KeyboardInterrupt) as error:
                with adapter.application(self.current, self.built, runtime, plain=False):
                    raise primary
        self.assertIs(error.exception, primary)
        stop.assert_called_once(); verify.assert_called_once(); final.assert_called_once()
        self.assertEqual(len(primary.__notes__), 3)

    def test_cleanup_failure_without_body_error_prevents_success(self) -> None:
        runtime = self.base / 'plain'
        runtime.mkdir()
        with patch.object(adapter.rust_launch, 'start', return_value=Mock(pid=101)), \
                patch.object(adapter.benchmark, 'stop', side_effect=OSError('stop failed')), \
                patch.object(adapter.rust_build, 'verify') as verify, \
                patch.object(adapter, 'final_health') as final, self.assertRaisesRegex(OSError, 'stop failed'):
            with adapter.application(self.current, self.built, runtime, plain=True):
                pass  # A successful body must still fail if the process cannot stop.
        verify.assert_called_once(); final.assert_not_called()

    def test_wrapped_child_exposes_actual_pipes_and_lifecycle(self) -> None:
        owner = Mock(pid=101, stdin=io.StringIO(), stdout=io.StringIO())
        child = adapter.Application(owner, 1)
        self.assertEqual(child.pid, 101)
        self.assertIs(child.stdin, owner.stdin); self.assertIs(child.stdout, owner.stdout)
        child.poll(); child.wait(timeout=3); child.kill()
        owner.poll.assert_called_once(); owner.wait.assert_called_once_with(timeout=3); owner.kill.assert_called_once()

    @contextmanager
    def measured_application(self, *args, **kwargs):
        self.output.mkdir(exist_ok=True)
        runtime = self.output / ('plain' if kwargs['plain'] else 'instrumented')
        runtime.mkdir(exist_ok=True)
        yield SimpleNamespace(pid=101, created_at_monotonic_ns=1, first_response=None)

    def test_bad_startup_or_warmup_checksum_never_reaches_measured_samples(self) -> None:
        good = {'checksum': benchmark.checksum(1), 'calls': 1, 'elapsed_ns': 10}
        for index, responses in enumerate(([{**good, 'checksum': -1}], [good, {**good, 'checksum': -1}])):
            self.output = self.base / f'results-{index}'
            with self.subTest(responses=responses), patch.object(adapter, 'application', self.measured_application), \
                    patch.object(adapter.benchmark, 'batch', side_effect=responses), \
                    patch.object(adapter.benchmark_process, 'measure_batches') as sample, self.assertRaisesRegex(ValueError, 'checksum'):
                adapter.measure(self.current, self.built, self.output, plain=True, records=benchmark.Measurements())
            sample.assert_not_called()

    def test_rust_cli_requires_build_and_routes_common_comparison_arguments(self) -> None:
        arguments = argparse.Namespace(language='rust', build=self.build, output=self.output,
            no_publish=True, comparison_id='same-workload')
        with patch.object(adapter, 'run', return_value={'language': 'rust'}) as run:
            self.assertEqual(run_benchmark(self.current, arguments), {'language': 'rust'})
        run.assert_called_once_with(self.current, self.output, build_directory=self.build, publish=False,
                                    comparison_id='same-workload')
        arguments.build = None
        with self.assertRaisesRegex(ValueError, 'requires --build'):
            run_benchmark(self.current, arguments)
        arguments.language = 'java'; arguments.build = self.build
        with self.assertRaisesRegex(ValueError, 'only to Rust'):
            run_benchmark(self.current, arguments)
        with patch('sys.argv', ['quux-otelc', '--config', 'examples/benchmark-metrics.toml',
                'benchmark', '--language', 'rust', '--output', str(self.output)]), \
                patch('sys.stderr', new_callable=io.StringIO) as errors, self.assertRaises(SystemExit) as exit_code:
            main()
        self.assertEqual(exit_code.exception.code, 1)
        self.assertIn('requires --build', errors.getvalue())

    def fake_measure(self, policy, built, output, *, plain, records):
        """Unit orchestration fixture; these literal samples are not Rust evidence."""
        pid = 101 if plain else 102
        modes = ['plain'] if plain else list(benchmark.STATES)
        for mode in modes:
            records.samples[mode].append(1.)
        with (output / 'raw.jsonl').open('a') as stream:
            stream.write(json.dumps({'fixture': True, 'pid': pid}) + '\n')
        if not plain:
            (output / 'metric-finalization.json').write_text('{}')
        return SimpleNamespace(pid=pid, first_response={'fixture': True}, evidence={}, sdk_report=None)

    def test_report_uses_common_format_and_embedded_sdk_without_extra_process(self) -> None:
        tool = Mock()
        tool.manifest.return_value = {'version': 'rustc 1.98.1\nLLVM version: 22.1.8\n'}
        with patch.object(adapter.rust_build, 'read', return_value=self.built), \
                patch.object(adapter.rust_build, 'verify') as verify, \
                patch.object(adapter.rust_compiler, 'read', return_value=tool), \
                patch.object(adapter, 'measure', side_effect=self.fake_measure), \
                patch.object(adapter.benchmark, 'export_result') as export:
            report = adapter.run(self.current, self.output, build_directory=self.build, comparison_id='fixture-only')
        verify.assert_called_once_with(self.built, self.current)
        export.assert_called_once_with(report, self.current.data['export']['endpoint'])
        self.assertEqual(report['language'], 'rust')
        self.assertEqual(report['comparison_id'], 'fixture-only')
        self.assertEqual(report['pids'], {'plain': 101, 'instrumented': 102})
        self.assertIsNone(report['sdk_pid'])
        self.assertIs(report['source_complete'], False)
        self.assertEqual(set(report['samples_ns_per_operation']), {'plain', *benchmark.STATES})
        self.assertEqual(json.loads((self.output / 'report.json').read_text()), report)

    def test_failed_measurement_changed_inputs_or_retained_evidence_prevent_publication(self) -> None:
        for kind in ('measurement', 'build', 'evidence'):
            self.output = self.base / kind
            def measure(*args, **kwargs):
                if kind == 'measurement':
                    raise ValueError('measurement failed')
                result = self.fake_measure(*args, **kwargs)
                if kind == 'evidence':
                    runtime = self.output / ('plain' if kwargs['plain'] else 'instrumented')
                    runtime.mkdir()
                    path = runtime / 'record.json'; path.write_text('original')
                    result.evidence = {'record.json': native_gate.identity(path)}
                    path.write_text('changed')
                return result
            with self.subTest(kind=kind), patch.object(adapter.rust_build, 'read', return_value=self.built), \
                    patch.object(adapter.rust_build, 'verify', side_effect=ValueError('build changed')), \
                    patch.object(adapter, 'measure', side_effect=measure), \
                    patch.object(adapter.benchmark, 'export_result') as export, self.assertRaises(ValueError):
                adapter.run(self.current, self.output, build_directory=self.build)
            export.assert_not_called()
            self.assertFalse((self.output / 'report.json').exists())
