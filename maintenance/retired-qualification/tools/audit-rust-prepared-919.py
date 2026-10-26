"""Audit original hosted data and frozen sources without running retained code."""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

BASE = Path('/Users/sclarke/github/otelc-qualification')
ROOT = BASE / 'work/rust-receipt-duration-accounting'
OUT = BASE / '2026-10-26/rust-prepared-constructor-section'
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from ci import rust_telemetry
from otelc.config import Policy
HEAD = '9193bf18abae6df589db2821097b09c6af2292ca'
source = json.loads((OUT / 'source/source.json').read_text())
frozen = Path(source['root'])
assert source['head'] == HEAD
assert hashlib.sha256(Path(source['archive']).read_bytes()).hexdigest() == source['archive_sha256']
for name, facts in source['source_facts'].items():
    path = frozen / name
    st = path.stat()
    assert [hashlib.sha256(path.read_bytes()).hexdigest(), st.st_dev, st.st_ino, st.st_mode,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns] == facts
    assert [st.st_uid, st.st_gid, st.st_nlink] == source['extra_facts'][name]

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
for host, job, artifact in (('linux', 121379947003, 12624929314), ('macos', 121379947220, 12625139566)):
    path = OUT / (host + '-original.zip')
    with zipfile.ZipFile(path) as archive:
        assert archive.read('head.txt').decode().strip() == HEAD
        log = archive.read('tests.log').decode()
        assert 'Ran 136 tests in ' in log
        assert ('OK (skipped=1)' if host == 'linux' else '\nOK\n') in log
        assert '\nFAILED' not in log and '\nERROR:' not in log
        coverage = json.loads(archive.read('python-coverage.json'))['files']
        assert len(coverage) == 8 and all(v['summary']['percent_covered'] >= 80 for v in coverage.values())
        capture = SimpleNamespace(metrics=[], traces=[])
        for name in archive.namelist():
            if name.startswith('public-case/decoded-metrics-'):
                capture.metrics.append(ExportMetricsServiceRequest.FromString(archive.read(name)))
            elif name.startswith('public-case/decoded-traces-'):
                capture.traces.append(ExportTraceServiceRequest.FromString(archive.read(name)))
        results = []
        for case, function, spans, calls, lifetimes in (
            ('constructor_argv_and_same_pid_live_metric_states', 'app.rs::workload', 4, 2, {}),
            ('drop_profile_preserves_plain_and_instrumented_outcomes', 'LifetimeApp.rs::workload', 1, 1,
             {'Item lifetime': 1, 'Zero lifetime': 2}),
        ):
            for plain in (False, True):
                prefix = 'public-case/test_prepared_real_' + case + '/' + str(plain) + '/'
                records = {name: json.loads(archive.read(prefix + 'rust-' + name + '.json')) for name in (
                    'request', 'plan', 'prepared', 'application-start', 'supervisor-start', 'host-release', 'execution')}
                request, plan, prepared, app, supervisor, release, result = records.values()
                assert request['plain'] is plain and request['arguments'] == ['literal $value; *']
                assert result['inputs_rechecked'] is True and result['failures'] == {}
                assert result['released'] is True and result['exec_verified'] is False
                assert result['process_exit_code'] == result['supervisor_exit_code'] == 0
                assert result['application'] == app and result['supervisor'] == supervisor
                assert app['parent_pid'] == app['supervisor_pid'] == supervisor['pid']
                assert app['pid'] == app['process_group'] == app['session']
                assert app['phase'] == 'waiting_exec' and app['source_complete'] is False
                assert len({request['launch_id'], app['launch_id'], supervisor['launch_id']}) == 1
                for key in ('request', 'plan'):
                    encoded = json.dumps(records[key], sort_keys=True, separators=(',', ':')).encode()
                    digest = hashlib.sha256(encoded).hexdigest()
                    assert prepared[key + '_sha256'] == result[key + '_sha256'] == digest
                for row in prepared['producer']:
                    relative = row['path'].split('/otelc/otelc/', 1)[-1]
                    assert row['sha256'] == source['source_facts'][relative][0], relative
                if plain:
                    assert result['sdk_health'] is None
                else:
                    health = json.loads(archive.read(prefix + 'app/health.json'))
                    assert health == result['sdk_health'] and health['pid'] == app['pid']
                    results.append(rust_telemetry.verify(capture, Policy.parse(request['policy']), health,
                        function=function, expected_spans=spans, expected_calls=calls, lifetimes=lifetimes))
        value = {'head': HEAD, 'run': 40262882720, 'job': job, 'artifact': artifact,
            'original_zip_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'original_log_sha256': hashlib.sha256((OUT / (host + '-original.log')).read_bytes()).hexdigest(),
            'source_facts_checked': len(source['source_facts']), 'tests': 136,
            'skips': 1 if host == 'linux' else 0,
            'coverage': {n: v['summary'] for n, v in coverage.items()}, 'fixture_receipts': results,
            'local_application_starts': 0, 'local_sdk_builds': 0, 'local_compiler_builds': 0,
            'scope': 'Actual hosted suite plus retained prepared/API records and decoded reserializations. '
                     'No retained image execution, original-wire proof, current395 runtime claim, '
                     'independent provider admission or complete compiler/source/dependency closure.',
            'reader_correction': 'Initial local audit compared canonical record digests to pretty-printed raw file hashes. '
                                 'Corrected to independently canonicalize sorted compact JSON as specified by the retained format; '
                                 'all source and receipt checks unchanged.'}
        (OUT / (host + '-audit.json')).write_text(json.dumps(value, indent=2) + '\n')
        print(host, value['head'], value['tests'], 'eight floors >=80', 'two exact API receipt joins')
