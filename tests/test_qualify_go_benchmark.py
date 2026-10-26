"""Retained-run orchestration refuses source changes and preserves failures."""
from __future__ import annotations

from contextlib import contextmanager
import gzip
import json
from pathlib import Path
import signal
import subprocess
import tarfile
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from ci import qualify_go_benchmark as qualifier
from otelc import file_inputs
import test_go_benchmark_receipt as fixtures


class GoBenchmarkQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.output = self.base / 'output'
        self.output.mkdir()
        self.capture = self.base / 'oteleq'
        (self.capture / 'examples').mkdir(parents=True)
        self.module = ModuleType('otelc_capture_forward')
        self.module.__file__ = str(self.capture / 'examples/otelc_capture_forward.py')

    def test_common_policy_starts_off_without_changing_the_original_template(self) -> None:
        original = (qualifier.ROOT / 'examples/benchmark-metrics.toml').read_bytes()
        selected = qualifier.policy('http://127.0.0.1:12345', self.output / 'otelc.toml')
        self.assertFalse(selected.data['metrics']['collection'])
        self.assertFalse(selected.data['metrics']['emission'])
        self.assertFalse(selected.data['lifetimes']['enabled'])
        self.assertEqual(len(selected.data['languages']), 8)
        self.assertEqual(selected.data['export']['endpoint'], 'http://127.0.0.1:12345')
        expected = json.loads(json.dumps(qualifier.Policy.load(qualifier.ROOT / 'examples/benchmark-metrics.toml').data))
        expected['export']['endpoint'] = 'http://127.0.0.1:12345'
        expected['metrics'].update(collection=False, emission=False)
        self.assertEqual(selected.data, expected)
        self.assertEqual((qualifier.ROOT / 'examples/benchmark-metrics.toml').read_bytes(), original)
        with patch.object(qualifier, 'ROOT', self.base), self.assertRaises(FileNotFoundError):
            qualifier.policy('http://127.0.0.1:12345', self.output / 'missing.toml')

    def test_catalogue_checks_clean_git_revision_and_actual_original_facts(self) -> None:
        source = self.capture / 'a file.py'
        source.write_text('original')
        def git(root, *args):
            return b'a' * 40 + b'\n' if args[0] == 'rev-parse' else (b'' if args[0] == 'status' else b'a file.py\0')
        with patch.object(qualifier, 'git', side_effect=git):
            before = qualifier.catalogue(self.capture, 'a' * 40)
            self.assertEqual(set(before), {'a file.py'})
            qualifier.unchanged(self.capture, 'a' * 40, before)
            source.write_text('changed')
            with self.assertRaises(ValueError): qualifier.unchanged(self.capture, 'a' * 40, before)
        with patch.object(qualifier, 'git', return_value=b'wrong'), self.assertRaises(ValueError):
            qualifier.catalogue(self.capture, 'a' * 40)

    def test_new_external_or_fixed_build_outputs_preserve_source_boundaries(self) -> None:
        sdk = self.base / 'sdk'
        (sdk / 'examples/go').mkdir(parents=True)
        with patch.object(qualifier, 'ROOT', sdk):
            self.assertEqual(qualifier.destination(self.base / 'new', self.capture), self.base / 'new')
            self.assertEqual(qualifier.destination(sdk / 'build/new', self.capture), sdk / 'build/new')
            (sdk / 'build').mkdir()
            self.assertEqual(qualifier.destination(sdk / 'build/new', self.capture), sdk / 'build/new')
            alias = self.base / 'alias'
            alias.symlink_to(sdk, target_is_directory=True)
            for output in (sdk, sdk / 'examples/new', alias / 'examples/new', self.capture / 'new'):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    qualifier.destination(output, self.capture)

    def test_command_uses_literal_argv_keeps_exit_evidence_and_refuses_low_space(self) -> None:
        process = Mock(pid=123, returncode=0)
        process.wait.return_value = 0
        with patch.object(qualifier.shutil, 'disk_usage', return_value=Mock(free=qualifier.FLOOR)), \
                patch.object(qualifier.subprocess, 'Popen', return_value=process) as start:
            qualifier.execute(['not-executed', '$(literal)'], self.output, 'command')
        self.assertEqual(start.call_args.args[0], ['not-executed', '$(literal)'])
        self.assertNotIn('shell', start.call_args.kwargs)
        self.assertEqual(json.loads((self.output / 'command.command.json').read_text())['driver_pid'], 123)
        with patch.object(qualifier.shutil, 'disk_usage', return_value=Mock(free=qualifier.FLOOR - 1)), \
                patch.object(qualifier.subprocess, 'Popen', side_effect=AssertionError('must not start')), \
                self.assertRaises(ValueError):
            qualifier.execute(['not-executed'], self.output, 'low')

    def test_timeout_cleanup_and_record_failure_keep_the_original_exception(self) -> None:
        primary = subprocess.TimeoutExpired('driver', 900)
        process = Mock(pid=123, returncode=-9)
        process.wait.side_effect = [primary, subprocess.TimeoutExpired('driver', 30), -9]
        process.poll.return_value = None
        with patch.object(qualifier.shutil, 'disk_usage', return_value=Mock(free=qualifier.FLOOR)), \
                patch.object(qualifier.subprocess, 'Popen', return_value=process), \
                patch.object(qualifier.downstream, 'save', side_effect=OSError('record failed')), \
                self.assertRaises(subprocess.TimeoutExpired) as caught:
            qualifier.execute(['not-executed'], self.output, 'timeout')
        self.assertIs(caught.exception, primary)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 3)
        self.assertEqual(len(primary.__notes__), 2)
        process = Mock(pid=123, returncode=7)
        process.wait.return_value = 7
        process.poll.return_value = 7
        with patch.object(qualifier.shutil, 'disk_usage', return_value=Mock(free=qualifier.FLOOR)), \
                patch.object(qualifier.subprocess, 'Popen', return_value=process), \
                self.assertRaises(subprocess.CalledProcessError):
            qualifier.execute(['not-executed'], self.output, 'failed')
        process.send_signal.assert_not_called()

    def test_fixed_launch_records_exclude_live_control_and_refuse_changed_sources(self) -> None:
        comparison = self.base / 'comparison'
        evidence = {'launch_evidence': {}}
        common = {qualifier.prepared_go.REQUEST, qualifier.prepared_go.PLAN, qualifier.prepared_go.PREPARED,
                  'go-supervisor-start.json', 'go-application-start.json', 'go-execution.json',
                  'process.json', 'go-first-response.json'}
        for lane in ('plain', 'instrumented'):
            folder = comparison / lane
            folder.mkdir(parents=True)
            names = common | ({'config.json', 'health.json', 'go-metric-finalization.json'}
                              if lane == 'instrumented' else set())
            evidence['launch_evidence'][lane] = {}
            for name in names:
                path = folder / name
                path.write_bytes(b'original record')
                evidence['launch_evidence'][lane][name] = file_inputs.identity(path)
            (folder / 'control.json').write_bytes(b'private endpoint omitted')
        for name in ('report.json', 'raw.jsonl', 'responses.jsonl'):
            (comparison / name).write_bytes(b'original')
        qualifier.records(comparison, self.output, evidence)
        self.assertFalse(any('control.json' in p.name for p in self.output.iterdir()))
        (comparison / 'plain/process.json').write_bytes(b'changed')
        second = self.base / 'unused'
        second.mkdir()
        with self.assertRaises(ValueError): qualifier.records(comparison, second, evidence)

    def test_retained_images_and_source_trees_omit_compiler_caches(self) -> None:
        build = self.base / 'build'
        (build / 'inspection').mkdir(parents=True)
        (build / 'inspection/inventory.json').write_text('{}')
        (build / 'build.json').write_text('{}')
        for lane in ('plain', 'instrumented'):
            (build / lane).write_bytes(b'original image bytes, not executed')
        for folder in ('private', 'sdk', 'context', 'compiler-temp', 'weaver-work'):
            (build / folder).mkdir()
            (build / folder / 'input.go').write_text('package main')
        qualifier.products(build, self.output)
        self.assertEqual(gzip.decompress((self.output / 'plain-image.gz').read_bytes()), b'original image bytes, not executed')
        with tarfile.open(self.output / 'private-inputs.tar.gz') as archive:
            names = archive.getnames()
        self.assertTrue(any(name.startswith('sdk/') for name in names))
        self.assertFalse(any('compiler-temp' in name or 'weaver-work' in name for name in names))

    def test_stack_readiness_is_distinct_from_benchmark_receipt(self) -> None:
        def response(*args, **kwargs):
            value = Mock(status=200)
            value.__enter__ = Mock(return_value=value)
            value.__exit__ = Mock(return_value=False)
            return value
        with patch.object(qualifier.urllib.request, 'urlopen', side_effect=response) as opened:
            qualifier.ready()
        self.assertEqual(opened.call_count, 3)
        with patch.object(qualifier.urllib.request, 'urlopen', side_effect=OSError('offline')), \
                patch.object(qualifier.time, 'monotonic', side_effect=[0., 61.]), self.assertRaises(OSError):
            qualifier.ready()

    def test_wrapper_rechecks_both_sources_on_failure_and_never_publishes_success(self) -> None:
        primary = RuntimeError('measurement failed')
        self.module.__file__ = str(self.capture / 'examples/otelc_capture_forward.py')
        old_path = qualifier.sys.path[:]
        self.addCleanup(lambda: qualifier.sys.path.__setitem__(slice(None), old_path))
        with patch.object(qualifier, 'git', return_value=b'a' * 40), \
                patch.object(qualifier, 'catalogue', return_value={}), \
                patch.object(qualifier.subprocess, 'run'), \
                patch.object(qualifier.importlib, 'import_module', return_value=self.module), \
                patch.object(qualifier, 'perform', side_effect=primary), \
                patch.object(qualifier, 'unchanged', side_effect=OSError('source changed')) as check, \
                self.assertRaises(RuntimeError) as caught:
            qualifier.run(self.capture, 'b' * 40, self.base / 'failed', 'fresh-run')
        self.assertIs(caught.exception, primary)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(len(primary.__notes__), 2)
        self.assertFalse((self.base / 'failed/evidence/qualification.json').exists())

    def test_public_command_lanes_drain_capture_before_downstream_checks(self) -> None:
        fixture = fixtures.GoBenchmarkReceiptTests()
        fixture.setUp()
        captured = Mock(endpoint='http://127.0.0.1:12345')
        closed = []
        @contextmanager
        def forwarding(*args, **kwargs):
            yield captured
            closed.append(True)
        self.module.forwarding_receiver = forwarding
        output = self.base / 'pipeline'
        output.mkdir()
        evidence = output / 'evidence'
        evidence.mkdir()
        def policy(endpoint, path):
            path.write_text('schema_version=3\n')
            return fixture.policy
        commands = []
        def execute(argv, directory, label):
            commands.append(argv)
            if label == 'benchmark':
                folder = output / 'comparison'
                folder.mkdir()
                (folder / 'report.json').write_text(json.dumps(fixture.value))
                for name, rows in (('raw.jsonl', fixture.raw), ('responses.jsonl', fixture.responses)):
                    (folder / name).write_text(''.join(json.dumps(row) + '\n' for row in rows))
        def observed(*args):
            self.assertEqual(closed, [True])
            captured.require_complete.assert_called_once()
            return {'actual query fixture': True}
        with patch.object(qualifier, 'ready'), patch.object(qualifier, 'policy', side_effect=policy), \
                patch.object(qualifier, 'execute', side_effect=execute), \
                patch.object(qualifier, 'records'), patch.object(qualifier, 'products'), \
                patch.object(qualifier.prepared_go, 'verify_build', return_value={}), \
                patch.object(qualifier.downstream, 'check', side_effect=observed):
            result = qualifier.perform(self.module, output, evidence, 'fresh-run')
        self.assertEqual(result['retained']['expected_checksum'], 41284864)
        self.assertEqual(commands[0][0:3], commands[1][0:3])
        self.assertIn('--scope-safe', commands[0])
        self.assertIn('benchmark', commands[1])


if __name__ == '__main__':
    unittest.main()
