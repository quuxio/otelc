"""Read retained Linux native benchmark evidence without running its products."""
from __future__ import annotations

from collections import defaultdict
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


BASE = Path('/Users/sclarke/github/otelc-qualification')
OUTPUT = BASE / '2026-10-26/native-benchmark-failure-capture/hosted'
HEAD = '9c7e4ada9b4646626291d37118ca5fbb935abc9d'
JOB = 121346663059
RUN = 40252308142
ARTIFACT = 12621268365


def digest(raw: bytes) -> str:
    """Return the identity of retained bytes."""
    return hashlib.sha256(raw).hexdigest()


def api(path: str) -> bytes:
    """Read GitHub using the verified quuxio helper."""
    flags = ['--allow-escape-sequences'] if path.endswith('/logs') else []
    return subprocess.check_output([
        '/usr/bin/python3', str(BASE / 'tools/quux-remote.py'), 'api',
        'repos/quuxio/otelc/' + path, *flags,
    ])


def source_facts(manifest: dict) -> int:
    """Require the original frozen source and archive to remain unchanged."""
    root = Path(manifest['root'])
    for name, expected in manifest['source_facts'].items():
        path = root / name
        stat = path.stat()
        assert [digest(path.read_bytes()), stat.st_dev, stat.st_ino, stat.st_mode,
                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns] == expected, name
        assert [stat.st_uid, stat.st_gid, stat.st_nlink] == manifest['extra_facts'][name], name
    assert digest(Path(manifest['archive']).read_bytes()) == manifest['archive_sha256']
    return len(manifest['source_facts'])


def audit() -> dict:
    """Join the successful job to original archive, reports and raw HTTP entities."""
    metadata = json.loads(api(f'actions/artifacts/{ARTIFACT}'))
    job = json.loads(api(f'actions/jobs/{JOB}'))
    assert metadata['workflow_run']['head_sha'] == HEAD
    assert metadata['workflow_run']['id'] == job['run_id'] == RUN
    assert job['status'] == 'completed' and job['conclusion'] == 'success'
    raw_archive = (OUTPUT / 'linux-original.zip').read_bytes()
    assert len(raw_archive) == metadata['size_in_bytes']
    assert metadata['digest'] == 'sha256:' + digest(raw_archive)
    manifest = json.loads((OUTPUT.parent / 'source-final/source.json').read_text())
    held_before = source_facts(manifest)
    log = api(f'actions/jobs/{JOB}/logs')
    log_path = OUTPUT / 'linux-original.log.gz'
    if log_path.exists():
        assert gzip.decompress(log_path.read_bytes()) == log
    else:
        with log_path.open('xb') as stream:
            stream.write(gzip.compress(log, mtime=0))
    with zipfile.ZipFile(io.BytesIO(raw_archive)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert archive.read('native-benchmark-diagnostics/head.txt').decode().strip() == HEAD
        assert not archive.read('native-benchmark-diagnostics/source-status.txt')
        assert not any(name.endswith('failed-flush.json') for name in names)
        source_tar = archive.read('native-benchmark-diagnostics/source.tar')
        assert digest(source_tar) == manifest['archive_sha256']
        with tarfile.open(fileobj=io.BytesIO(source_tar)) as source:
            members = [member for member in source.getmembers() if member.isfile()]
            assert len(members) == held_before
            assert {member.name for member in members} == set(manifest['source_facts'])
            for member in members:
                assert digest(source.extractfile(member).read()) == manifest['source_facts'][member.name][0]
        tests = json.loads(archive.read('native-benchmark-diagnostics/tests-result.json'))
        assert tests == {'selected': 10, 'tests_run': 10, 'skipped': 0, 'successful': True}
        coverage = json.loads(archive.read('native-benchmark-diagnostics/coverage.json'))
        assert coverage['files'] and all(row['summary']['percent_covered'] >= 80
                                         for row in coverage['files'].values())
        groups = defaultdict(lambda: {'requests': 0, 'spans': [], 'metrics': []})
        for name in names:
            if '/otlp-' not in name or not name.endswith('.json'):
                continue
            record = json.loads(archive.read(name))
            assert set(record) == {'schema_version', 'sequence', 'received_at_unix_ns',
                                   'path', 'content_encoding', 'body_bytes', 'body_sha256', 'scope'}
            entity = archive.read(name[:-5] + '.http-body')
            assert len(entity) == record['body_bytes'] and digest(entity) == record['body_sha256']
            assert record['schema_version'] == 1
            if record['content_encoding'] == 'gzip':
                payload = gzip.decompress(entity)
            else:
                assert record['content_encoding'] == ''
                payload = entity
            group = groups[name.rsplit('/', 1)[0].split('/')[-1].removesuffix('-otlp')]
            group['requests'] += 1
            if record['path'] == '/v1/traces':
                request = ExportTraceServiceRequest.FromString(payload)
                group['spans'].extend(span for resource in request.resource_spans
                                      for scope in resource.scope_spans for span in scope.spans)
            else:
                assert record['path'] == '/v1/metrics'
                request = ExportMetricsServiceRequest.FromString(payload)
                group['metrics'].extend(metric for resource in request.resource_metrics
                                        for scope in resource.scope_metrics for metric in scope.metrics)
        reports = []
        expected_spans = defaultdict(int)
        for name in names:
            if not name.endswith('/report.json'):
                continue
            report = json.loads(archive.read(name))
            parent = name.rsplit('/', 1)[0]
            count = report['config']['benchmark']['samples']
            assert set(report['samples_ns_per_operation']) == {
                'plain', 'both_off', 'collection_only', 'emission_only', 'both_on'}
            assert all(len(values) == count for values in report['samples_ns_per_operation'].values())
            assert len(report['transitions']) == 4 * count
            assert {row['pid'] for row in report['transitions']} == {report['pids']['instrumented']}
            assert report['sdk_pid'] is None and report['source_complete'] is False
            for row in report['transitions']:
                assert (row['collection'], row['emission']) == {
                    'both_off': (False, False), 'collection_only': (True, False),
                    'emission_only': (False, True), 'both_on': (True, True),
                }[row['mode']]
            rows = [json.loads(line) for line in archive.read(parent + '/raw.jsonl').splitlines()]
            assert len(rows) == 5 * count and rows == report['process_samples']
            assert all(row['checksum'] == report['expected_checksum'] and
                       row['calls'] == report['iterations'] for row in rows)
            execution = json.loads(archive.read(parent + '/instrumented/native-execution.json'))
            plain = json.loads(archive.read(parent + '/plain/native-benchmark-execution.json'))
            assert not execution['failures'] and execution['process_exit_code'] == 0
            assert execution['application']['pid'] == report['pids']['instrumented']
            assert plain['returncode'] == 0 and plain['pid'] == report['pids']['plain']
            image = archive.read(parent + '/compiled/instrumented')
            assert digest(image) == execution['application']['image']['sha256']
            health = json.loads(archive.read(parent + '/instrumented/app/health.json'))
            assert health == execution['sdk_health'] == report['sdk_health']
            assert health['lifecycle'] == {'flush': 'succeeded', 'shutdown': 'succeeded'}
            assert health['metric_flush']['failed_attempts'] == '0'
            for pipeline in ('metric_exports', 'trace_exports'):
                assert all(health[pipeline][key] == '0'
                           for key in ('batches_failed', 'batches_pending', 'units_failed'))
            for key in ('lost', 'invalid', 'metadata_errors', 'active'):
                assert health['function_accounting'][key] == '0'
            assert health['span_accounting']['queue_dropped'] == health['span_accounting']['queue_pending'] == '0'
            group_name = parent.split('/')[-1].rsplit('-', 1)[0]
            if parent.endswith('/cli-output'):
                group_name = 'test_public_cli_selects_c_with_the_same_policy_and_matching_sdk'
            expected_spans[group_name] += int(health['trace_exports']['units_succeeded'])
            reports.append({'path': name, 'language': report['language'], 'iterations': report['iterations'],
                            'samples_per_mode': count, 'object_lifetimes': report['config']['lifetimes']['enabled'],
                            'checksum': report['expected_checksum'], 'median_ns_per_operation': report['median_ns_per_operation']})
        assert len(reports) == 4
        for name, expected in expected_spans.items():
            spans = groups[name]['spans']
            assert len(spans) == expected
            assert len({(span.trace_id, span.span_id) for span in spans}) == expected
            assert all(span.end_time_unix_nano >= span.start_time_unix_nano > 0 for span in spans)
        life = groups['test_cpp_lifetime_cost_uses_the_common_policy_and_true_destruction']['spans']
        functions = {span.span_id for span in life if span.name != 'WorkItem lifetime'}
        lifetimes = [span for span in life if span.name == 'WorkItem lifetime']
        assert len(lifetimes) == 171
        assert all(len(span.links) == 1 and span.links[0].span_id in functions for span in lifetimes)
        sdk_name = next(name for name in names if name.endswith('/sdk/native-sdk-build.json'))
        sdk = json.loads(archive.read(sdk_name))
        library = sdk_name.rsplit('/', 1)[0] + '/libotelc_native_runtime.so'
        assert digest(archive.read(library)) == sdk['library']['sha256']
        prefix = '/otelc/otelc/'
        for item in sdk['sources']:
            relative = item['path'].split(prefix, 1)[1]
            assert item['sha256'] == manifest['source_facts'][relative][0]
        summary = {name: {'http_entities': value['requests'], 'spans': len(value['spans']),
                          'metric_names': sorted({metric.name for metric in value['metrics']})}
                   for name, value in groups.items()}
        unexpected = [name for name in names if '/sdk/_deps/' in name]
    assert source_facts(manifest) == held_before
    return {'head': HEAD, 'run': RUN, 'job': JOB, 'artifact': metadata,
            'archive_sha256': digest(raw_archive), 'source_facts_held': held_before,
            'original_log_sha256': digest(log), 'tests': tests,
            'coverage': {name: row['summary'] for name, row in coverage['files'].items()},
            'reports': reports, 'raw_receiver_groups': summary, 'lifetime_spans': len(lifetimes),
            'unexpected_sdk_documentation_members': unexpected,
            'local_application_starts': 0, 'local_builds': 0,
            'limits': 'Exact-head Linux benchmark corpus only. Historical process/stat records are data, '
                      'not freshly recertified. Raw entities prove receipt before response, not SDK acknowledgement. '
                      'macOS/full/Sonar gates, original flush root cause, complete source closure, independent '
                      'oteleq and cross-language performance acceptance remain separate.'}


if __name__ == '__main__':
    result = audit()
    with (OUTPUT / 'linux-audit.json').open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'status': 'pass', 'reports': len(result['reports']),
                      'lifetime_spans': result['lifetime_spans'],
                      'source_facts_held': result['source_facts_held']}))
