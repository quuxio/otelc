"""Reject incomplete/misparented stored telemetry and preserve unedited examples."""
import base64
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import trace_check as check

TRACE = '01' * 16
PARENT = '02' * 8
CHILD = '03' * 8


def encoded(value):
    return base64.b64encode(bytes.fromhex(value)).decode()


def tree():
    return {'batches': [{'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'test'}}]},
                         'scopeSpans': [{'scope': {'name': 'quux.otelc'}, 'spans': [
                             {'traceId': encoded(TRACE), 'spanId': encoded(PARENT), 'name': 'parent',
                              'startTimeUnixNano': '10', 'endTimeUnixNano': '30', 'status': {}},
                             {'traceId': encoded(TRACE), 'spanId': encoded(CHILD), 'parentSpanId': encoded(PARENT),
                              'name': 'child', 'startTimeUnixNano': '11', 'endTimeUnixNano': '29',
                              'status': {'code': 'STATUS_CODE_ERROR'}}]}]}]}


def health():
    return {'export_finished': True, 'export_loss': 0, 'losses': {}, 'function_calls': 2,
            'traces': {'active_trees': 0, 'queued_trees': 0, 'completed_trees': 1, 'losses': {}}}


class TraceCheckTests(unittest.TestCase):
    def test_http_json_and_id_validation(self):
        with patch.object(check.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"ok":true}')) as fetch:
            self.assertEqual(check.get_json('/ready'), {'ok': True})
            fetch.assert_called_once_with(check.TEMPO + '/ready', timeout=5)
        self.assertEqual(check.identity(encoded(PARENT), 8), PARENT)
        self.assertEqual(check.search_identity("1"), "0" * 31 + "1")
        for value in ("0", "", "x", "1" * 33, 1):
            with self.assertRaises(ValueError): check.search_identity(value)
        for value in ('!', '', encoded('00' * 8), encoded(TRACE)):
            with self.assertRaises(ValueError):
                check.identity(value, 8)

    def test_stored_parent_tree_and_errors(self):
        result = check.stored_tree(tree(), TRACE, 'test')
        self.assertEqual(result['trace_id'], TRACE)
        self.assertEqual(result['spans'][1]['parent'], PARENT)
        self.assertTrue(result['spans'][1]['error'])
        fixture = tree()
        fixture['batches'][0]['scopeSpans'][0]['spans'][1]['status']['code'] = 2
        self.assertTrue(check.stored_tree(fixture, TRACE, 'test')['spans'][1]['error'])

    def test_invalid_resources_scopes_ids_timestamps_and_parent_graphs(self):
        changes = [
            lambda d: d['batches'][0]['resource'].update(attributes=[]),
            lambda d: d['batches'][0]['scopeSpans'][0].update(scope={}),
            lambda d: d['batches'][0]['scopeSpans'][0]['spans'][1].update(spanId=encoded(PARENT)),
            lambda d: d['batches'][0]['scopeSpans'][0]['spans'][1].update(traceId=encoded('09' * 16)),
            lambda d: d['batches'][0]['scopeSpans'][0]['spans'][1].update(startTimeUnixNano='0'),
            lambda d: d['batches'][0]['scopeSpans'][0]['spans'][1].update(endTimeUnixNano='9'),
            lambda d: d['batches'][0]['scopeSpans'][0]['spans'][1].update(name=''),
            lambda d: d['batches'][0]['scopeSpans'][0]['spans'][1].pop('parentSpanId'),
            lambda d: d['batches'][0]['scopeSpans'][0]['spans'][1].update(parentSpanId=encoded('04' * 8)),
            lambda d: d['batches'][0]['scopeSpans'][0]['spans'][1].update(parentSpanId=encoded(CHILD)),
            lambda d: d['batches'][0]['scopeSpans'][0]['spans'][1].update(endTimeUnixNano='31'),
            lambda d: d['batches'][0]['scopeSpans'][0].update(spans=[]),
        ]
        for change in changes:
            fixture = tree(); change(fixture)
            with self.subTest(change=change), self.assertRaises(ValueError):
                check.stored_tree(fixture, TRACE, 'test')
        fixture = tree()
        nodes = fixture['batches'][0]['scopeSpans'][0]['spans']
        cyclic = copy.deepcopy(nodes[1]); cyclic.update(spanId=encoded('04' * 8), parentSpanId=encoded(CHILD))
        nodes[1]['parentSpanId'] = cyclic['spanId']; nodes.append(cyclic)
        with self.assertRaisesRegex(ValueError, 'cycle'):
            check.stored_tree(fixture, TRACE, 'test')

    def test_storage_retry_and_complete_error_counts(self):
        missing = urllib.error.HTTPError('local', 404, 'missing', {}, None)
        valid = {'traces': [{'traceID': TRACE}]}
        with patch.object(check, 'get_json', side_effect=[missing, {'traces': []}, valid, tree()]), patch.object(check.time, 'sleep'):
            self.assertEqual(len(check.wait_for_storage('test', 2, 1, 1, 1)), 1)

    def test_storage_rejects_extras_errors_bad_search_ids_and_other_http_failures(self):
        for payload, spans, trees, errors in (({'traces': [{'traceID': 'invalid'}]}, 2, 1, 1),
                                             ({'traces': [{'traceID': TRACE}, {'traceID': TRACE}]}, 2, 1, 1),
                                             ({'traces': [{'traceID': TRACE}]}, 1, 1, 1),
                                             ({'traces': [{'traceID': TRACE}]}, 2, 1, 0)):
            with patch.object(check, 'get_json', side_effect=[payload, tree()]), self.assertRaises(ValueError):
                check.wait_for_storage('test', spans, trees, errors, 1)
        with patch.object(check, 'get_json', side_effect=urllib.error.HTTPError('local', 500, 'failed', {}, None)), self.assertRaises(urllib.error.HTTPError):
            check.wait_for_storage('test', 2, 1, 1, 1)
        with patch.object(check, 'get_json', return_value={'traces': []}), patch.object(check.time, 'monotonic', side_effect=[0, 1]), self.assertRaises(TimeoutError):
            check.wait_for_storage('test', 2, 1, 1, 1)

    def test_health_rejects_loss_incomplete_export_missing_counts_and_contended_snapshot(self):
        check.report_complete(health(), 2, 1)
        for change in ({'export_finished': False}, {'drained': False}, {'export_loss': 1},
                       {'export_dropped_batches': 1}, {'losses': {'queue': 1}}, {'traces': None},
                       {'function_calls': 1}):
            fixture = health(); fixture.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError): check.report_complete(fixture, 2, 1)
        for key, value in (('losses', {'incomplete': 1}), ('active_trees', 1), ('queued_trees', 1), ('completed_trees', 0)):
            fixture = health(); fixture['traces'][key] = value
            with self.assertRaises(ValueError): check.report_complete(fixture, 2, 1)

    def test_subprocess_and_all_language_build_commands(self):
        with patch.object(check.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'output')) as run:
            self.assertEqual(check.execute([Path('/bin/app')], Path('/root'), {}), 'output')
            self.assertTrue(run.call_args.kwargs['check'])
            self.assertEqual(run.call_args.kwargs['timeout'], 180)
        with tempfile.TemporaryDirectory() as directory, patch.object(check, 'execute') as execute:
            root = Path(directory); folder = root / 'output'; folder.mkdir()
            metadata = root / 'target/debug'; metadata.mkdir(parents=True)
            (metadata / 'otelc-llvm-toolchain.json').write_text('{"bindir":"/llvm"}')
            for language in check.EXAMPLES:
                plain, instrumented = check.commands(root, folder, language, root / 'source', root / 'policy', {})
                self.assertTrue(plain)
                self.assertEqual(instrumented[:2], [root / 'target/debug/quux-otelc', '--config'])
            self.assertEqual(execute.call_count, 5)

    def example(self, directory, changed=False, mismatch=False):
        root = Path(directory); (root / 'examples/apps').mkdir(parents=True)
        (root / 'examples/apps/python_trace_app.py').write_text('print(42)')
        (root / 'examples/python-traces.toml').write_text('schema_version=2')
        report = health(); report['function_calls'] = 9; report['traces']['completed_trees'] = 5
        def execute(command, _root, environment):
            if command == ['instrumented']:
                Path(environment['OTELC_REPORT_PATH']).write_text(json.dumps(report))
                self.assertNotIn('OTEL_EXPORTER_OTLP_ENDPOINT', environment)
                self.assertEqual(environment['OTEL_SERVICE_NAME'], 'otelc-python-trace-check-run')
                if changed: (root / 'examples/apps/python_trace_app.py').write_text('changed')
                return 'wrong' if mismatch else '42'
            return '42'
        with patch.object(check, 'commands', return_value=(['plain'], ['instrumented'])), patch.object(check, 'execute', side_effect=execute), patch.object(check, 'wait_for_storage', return_value=[{'trace_id': TRACE, 'spans': []}]), patch.dict(os.environ, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'ambient'}):
            return check.run_example(root, root / 'output', 'python', 60, 'run')

    def test_examples_compare_results_hash_sources_and_never_hide_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.example(directory)
            self.assertEqual(result['output'], '42')
            self.assertEqual(len(result['source_sha256']), 64)
            self.assertIn('var-traceId=', result['dashboard'])
        for changed, mismatch in ((True, False), (False, True)):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                self.example(directory, changed, mismatch)

    def test_cli_summary_timeout_bounds_and_stale_success_removal(self):
        result = {'language': 'c', 'stored_trees': [{'spans': [{}, {}]}], 'dashboard': 'view'}
        with tempfile.TemporaryDirectory() as directory, patch.object(check, 'run_example', return_value=result) as run, patch('sys.argv', ['trace_check', '--language', 'c', '--output', directory]), patch('sys.stdout', new_callable=io.StringIO):
            check.main()
            self.assertTrue(json.loads((Path(directory) / 'results.json').read_text())['complete'])
            run.assert_called_once()
            with patch.object(check, 'run_example', side_effect=TimeoutError), self.assertRaises(TimeoutError): check.main()
            self.assertFalse((Path(directory) / 'results.json').exists())
        with tempfile.TemporaryDirectory() as directory, patch.object(check, 'run_example', return_value=result) as run, patch('sys.argv', ['trace_check', '--output', directory]), patch('sys.stdout', new_callable=io.StringIO):
            check.main(); self.assertEqual(run.call_count, 8)
        with patch('sys.argv', ['trace_check', '--timeout', '0']), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit): check.main()
