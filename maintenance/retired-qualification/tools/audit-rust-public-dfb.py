"""Audit final public Rust lifetime workflow outputs without executing products."""
from __future__ import annotations

import concurrent.futures
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

BASE = Path('/Users/sclarke/github/otelc-qualification/2026-10-25/rust-public-suite-budget')
OUTPUT = Path('/Users/sclarke/github/otelc-qualification/2026-10-26/rust-public-suite-budget/hosted')
HEAD = 'dfb950c9ad8f25b3ee53bfb000e3d03263cd3ce1'
HELPER = ['/usr/bin/python3', '/Users/sclarke/github/otelc-qualification/tools/quux-remote.py', 'api']


def api(path: str) -> bytes:
    """Fetch original evidence through identity-checked quuxio credentials."""
    flags = ['--allow-escape-sequences'] if path.endswith('/logs') else []
    return subprocess.check_output([*HELPER, 'repos/quuxio/otelc/' + path, *flags])


def keep(path: Path, data: bytes) -> None:
    """Never replace original archive or log bytes on a repeated observation."""
    if path.exists():
        assert path.read_bytes() == data, str(path)
    else:
        with path.open('xb') as stream:
            stream.write(data)


def audit(row: tuple[str, int, int]) -> dict:
    """Check exact-head results, compiler bindings and independently decoded spans."""
    host, artifact, job = row
    metadata = json.loads(api(f'actions/artifacts/{artifact}'))
    assert metadata['workflow_run']['head_sha'] == HEAD
    path = OUTPUT / f'public-{host}-original.zip'
    raw = path.read_bytes() if path.exists() else api(f'actions/artifacts/{artifact}/zip')
    assert len(raw) == metadata['size_in_bytes']
    assert metadata['digest'] == 'sha256:' + hashlib.sha256(raw).hexdigest()
    keep(path, raw)
    state = json.loads(api(f'actions/jobs/{job}'))
    assert state['run_id'] == metadata['workflow_run']['id'] == 40245269793
    assert state['status'] == 'completed' and state['conclusion'] == 'success', (host, state['status'])
    log_path = OUTPUT / f'public-{host}-original.log.gz'
    log = gzip.decompress(log_path.read_bytes()) if log_path.exists() else api(f'actions/jobs/{job}/logs')
    keep(log_path, gzip.compress(log, mtime=0))
    source = json.loads((BASE / 'source.json').read_bytes())
    with zipfile.ZipFile(path) as archive:
        assert archive.read('head.txt').decode().strip() == HEAD
        text = archive.read('tests.log').decode()
        match = re.search(r'Ran (\d+) tests in ([\d.]+)s', text)
        assert match and int(match[1]) == 95
        assert re.search(r'\nOK(?: \(skipped=1\))?\n', text)
        assert 'test_public_lifetime_capacity_and_trace_policy_reach_the_running_sdk' in text
        health = {name: json.loads(archive.read(name)) for name in archive.namelist() if name.endswith('/health.json')}
        assert len(health) == 21
        drops = {name: value for name, value in health.items() if value['schema_version'] == 5}
        assert len(drops) == 8
        capacity = health['public-case/rust-drop-limit-1/health.json']
        assert (capacity['drop_accounting']['admitted'], capacity['drop_accounting']['completed'],
                capacity['drop_accounting']['lost']) == ('1', '1', '2')
        off = health['public-case/rust-drop-limit-4096/health.json']
        assert (off['drop_accounting']['admitted'], off['drop_accounting']['completed'],
                off['drop_accounting']['lost'], off['span_accounting']['sampled_started'],
                off['trace_exports']['units_started']) == ('3', '3', '0', '0', '0')
        for name, value in drops.items():
            observed = value['drop_accounting']
            assert observed['admitted'] == observed['completed'] and observed['closed'] is observed['closing'] is True
            assert all(observed[key] == '0' for key in ('live', 'dropping', 'ending', 'censored', 'duplicates', 'invalid'))
            if name != 'public-case/rust-drop-limit-1/health.json':
                assert observed['lost'] == '0'
        builds = []
        for folder, example in (('public-case/lifetime-build', 'examples/rust/LifetimeApp.rs'),
                                ('public-case/lifetime-benchmark-build', 'examples/rust/BenchmarkApp.rs')):
            value = json.loads(archive.read(folder + '/rust-source-build.json'))
            assert value['schema_version'] == 2 and value['profile'] == 'rust1981-standalone-drop-v2'
            assert len(value['inputs']) == 1 and value['inputs'][0]['sha256'] == source['source_facts'][example][0]
            assert len(value['requests']) == len(value['inventories']) == 3
            request = json.loads(archive.read(folder + '/lifetime-weave-request.json'))
            inventory = json.loads(archive.read(folder + '/lifetime-inspect-inventory.json'))
            woven = json.loads(archive.read(folder + '/lifetime-weave-inventory.json'))
            assert woven['lifetimes'] == request['lifetimes'] and woven['lifetime_bodies'] == request['lifetime_bodies']
            for body in inventory['bodies']:
                identity = body['function']['identity']
                if identity in request['lifetimes']:
                    encoded = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
                    assert hashlib.sha256(encoded).hexdigest() == request['lifetime_bodies'][identity]
            builds.append({'build': folder, 'selected_locals': sum(map(len, request['lifetimes'].values())),
                           'original_source_sha256': value['inputs'][0]['sha256']})
        plans = [json.loads(archive.read(name)) for name in archive.namelist() if name.endswith('rust-plan-profile.json')]
        assert len(plans) == 4 and all(value['schema_version'] == 3 and 'token' not in value for value in plans)
        spans = {}
        channels = [name for name in archive.namelist() if name.startswith('public-case/decoded-traces-') and name.endswith('.protobuf')]
        assert channels
        for name in channels:
            request = ExportTraceServiceRequest.FromString(archive.read(name))
            for resource in request.resource_spans:
                names = [item.value.string_value for item in resource.resource.attributes if item.key == 'service.name']
                assert len(names) == 1
                spans.setdefault(names[0], []).extend(span for scope in resource.scope_spans for span in scope.spans)
        for start in ('00', '01', '10', '11'):
            values = spans['public-lifetimes-' + start]
            functions = {(span.trace_id, span.span_id) for span in values if span.name == 'LifetimeApp.rs::workload'}
            lifetimes = [span for span in values if span.name.endswith(' lifetime')]
            assert (len(values), len(functions), len(lifetimes)) == (16, 4, 12)
            exceptional = []
            for span in lifetimes:
                assert not span.parent_span_id and len(span.links) == 1
                assert (span.links[0].trace_id, span.links[0].span_id) in functions
                attributes = {item.key: item.value for item in span.attributes}
                assert attributes['boundary'].string_value == 'drop_entry'
                exceptional.append(attributes['otelc.drop.exception_exit'].bool_value)
            assert sorted(exceptional) == [False] * 11 + [True]
        assert len(spans['rust-drop-limit-1']) == 2
        assert not spans.get('rust-drop-limit-4096')
        reports = [json.loads(archive.read(name)) for name in archive.namelist() if name.endswith('/report.json')]
        assert len(reports) == 6
        for value in reports:
            assert value['iterations'] == 3 and len(value['transitions']) == 8
            assert {step['pid'] for step in value['transitions']} == {value['pids']['instrumented']}
            assert all(len(samples) == 2 for samples in value['samples_ns_per_operation'].values())
        coverage = json.loads(archive.read('python-coverage.json'))['files']
        assert len(coverage) == 5 and all(value['summary']['percent_covered'] >= 80 for value in coverage.values())
    record = {'head': HEAD, 'job': job, 'conclusion': state['conclusion'], 'artifact': metadata,
              'artifact_sha256': hashlib.sha256(raw).hexdigest(), 'original_log_sha256': hashlib.sha256(log).hexdigest(),
              'methods': 95, 'skipped': int('(skipped=1)' in text), 'seconds': float(match[2]),
              'health_reports': 21, 'drop_health_reports': 8, 'token_free_actual_plans': 4,
              'builds_with_recomputed_complete_mir_fingerprints': builds, 'decoded_trace_channels': len(channels),
              'all_four_states_each': {'function_spans': 4, 'lifetime_spans': 12, 'exceptional_drop_spans': 1},
              'capacity_one': {'completed': 1, 'lost': 2, 'decoded_spans': 2},
              'traces_off': {'completed': 3, 'lost': 0, 'decoded_spans': 0},
              'benchmark_smoke_reports': 6, 'coverage_percent': {name: value['summary']['percent_covered'] for name, value in coverage.items()},
              'scope': 'Exact final public root-local lifetime workflows and benchmark smoke; decoded reserializations are not original HTTP bodies; not performance/Grafana, whole-language or oteleq admission'}
    keep(OUTPUT / f'public-{host}-audit.json', (json.dumps(record, indent=2) + '\n').encode())
    print(json.dumps({'host': host, 'methods': 95, 'minimum_python_percent': min(record['coverage_percent'].values()),
                      'capacity_loss_verified': 2, 'trace_off_decoded_spans': 0}), flush=True)
    return record


if __name__ == '__main__':
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(audit, [('linux', 12618517206, 121325387362), ('macos', 12619576090, 121325387467)]))
