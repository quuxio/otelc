"""Audit original hosted benchmark bytes without executing retained applications."""
from collections import Counter
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import statistics
import sys
import tarfile
import zipfile

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest, ExportMetricsServiceResponse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse

folder = Path(sys.argv[1]).resolve()
sdk_manifest = json.loads(Path(sys.argv[2]).read_bytes())
capture_manifest = json.loads(Path(sys.argv[3]).read_bytes())
run = json.loads((folder / 'run-terminal.json').read_bytes())
jobs = json.loads((folder / 'jobs-terminal.json').read_bytes())['jobs']
artifacts = json.loads((folder / 'artifacts.json').read_bytes())['artifacts']
assert run['status'] == 'completed' and run['conclusion'] == 'success'
assert run['head_sha'] == sdk_manifest['head']
assert len(jobs) == 3 and all(j['conclusion'] == 'success' for j in jobs)
assert len(artifacts) == 1
archive = folder / 'original.zip'
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
assert artifacts[0]['digest'] == 'sha256:' + digest


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def attributes(rows) -> dict:
    return {a.key: a.value.string_value for a in rows}


def workload_checksum(count: int) -> int:
    total = 0
    for seed in range(count):
        value = seed
        for _ in range(128):
            value = (value * 1664525 + 1013904223) & 65535
            total += value
    return total


with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
    names = z.namelist()
    assert len(names) == len(set(names))
    assert not any('control.json' in n or 'compiler-temp' in n or 'weaver-work' in n for n in names)
    read_json = lambda name: json.loads(z.read(name))
    accepted = read_json('qualification.json')
    assert accepted['otelc_head'] == sdk_manifest['head']
    assert accepted['oteleq_head'] == capture_manifest['head']
    identities = read_json('source-identities.json')
    source_counts = {}
    for kind, manifest, filename in (('otelc', sdk_manifest, 'otelc-source.tar.gz'),
                                    ('oteleq', capture_manifest, 'oteleq-source.tar.gz')):
        source = identities[kind]
        assert source['head'] == manifest['head']
        assert set(source['files']) == set(manifest['source_facts'])
        count = 0
        with tarfile.open(fileobj=io.BytesIO(z.read(filename)), mode='r:gz') as tar:
            assert tar.pax_headers.get('comment') == manifest['head']
            for member in tar:
                if not member.isfile():
                    continue
                raw = tar.extractfile(member).read()
                expected = manifest['source_facts'][member.name]
                recorded = source['files'][member.name]
                assert sha(raw) == expected[0] == recorded['sha256']
                assert len(raw) == expected[4] == recorded['size']
                count += 1
        assert count == len(source['files'])
        source_counts[kind] = count

    report = read_json('report.json')
    samples = [json.loads(line) for line in z.read('raw.jsonl').splitlines()]
    responses = [json.loads(line) for line in z.read('responses.jsonl').splitlines()]
    config = read_json('instrumented-config.json')
    health = read_json('instrumented-health.json')
    policy = report['config']
    iterations = policy['benchmark']['iterations']
    each = policy['benchmark']['samples']
    warmup = policy['benchmark']['warmup']
    assert len(policy['languages']) == 8 and iterations == 10000 and each == 15
    checksum = workload_checksum(iterations)
    assert report['expected_checksum'] == checksum
    assert len(samples) == len(responses) == each * 5
    assert samples == report['process_samples']
    modes = {'plain', 'both_off', 'collection_only', 'emission_only', 'both_on'}
    pids, owners = report['pids'], report['supervisor_pids']
    assert len(set([*pids.values(), *owners.values()])) == 4
    assert report['sdk_pid'] == pids['instrumented'] == health['pid']
    timing = {mode: [] for mode in modes}
    denominator = Counter()
    for row, response in zip(samples, responses, strict=True):
        mode, sample = row['mode'], row['sample']
        denominator[(mode, sample)] += 1
        lane = 'plain' if mode == 'plain' else 'instrumented'
        assert row['pid'] == response['pid'] == pids[lane]
        assert row['sample'] == response['sample'] and mode == response['mode']
        body = {k: row[k] for k in ('calls', 'checksum', 'elapsed_ns')}
        assert body == response['response'] and body['calls'] == iterations and body['checksum'] == checksum
        assert type(body['elapsed_ns']) is int and body['elapsed_ns'] > 0
        timing[mode].append(body['elapsed_ns'] / iterations)
        assert set(row['process_usage']) == {'application', 'supervisor'}
        for role, pid in (('application', pids[lane]), ('supervisor', owners[lane])):
            usage = row['process_usage'][role]
            before, after = usage['before'], usage['after']
            assert before['pid'] == after['pid'] == pid
            assert after['observed_at_unix_ns'] >= before['observed_at_unix_ns']
            assert usage['cpu_ns'] == after['cpu_ns'] - before['cpu_ns'] >= 0
            assert usage['rss_bytes'] == max(before['rss_bytes'], after['rss_bytes']) > 0
    assert denominator == Counter({(mode, sample): 1 for mode in modes for sample in range(each)})
    assert timing == report['samples_ns_per_operation']
    assert {m: statistics.median(v) for m, v in timing.items()} == report['median_ns_per_operation']
    states = {'both_off': (False, False), 'collection_only': (True, False),
              'emission_only': (False, True), 'both_on': (True, True)}
    assert len(report['transitions']) == each * 4
    for transition in report['transitions']:
        assert transition['pid'] == pids['instrumented']
        assert (transition['collection'], transition['emission']) == states[transition['mode']]
        assert transition['effective_emission_off'] == (not transition['emission'])
    function = 'io.quux.otelc.examples/go.workload'
    assert config['functions'] == [function]
    policy_bytes = json.dumps(policy, sort_keys=True, allow_nan=False).encode()
    assert config['policy_sha256'] == health['policy_sha256'] == sha(policy_bytes)
    assert report['config_sha256'] == sha(policy_bytes + b'\n')
    assert health['selection_sha256'] == config['selection_sha256']
    expected_invocations = 1 + (warmup + 4 * each) * iterations
    expected_collected = 2 * each * iterations
    probe = health['probe_accounting']
    assert probe['function_started'] == probe['function_ended'] == str(expected_invocations)
    assert all(probe[k] == '0' for k in ('function_lost', 'function_active', 'unwind_unknown'))
    assert health['phase'] == 'final' and health['lifecycle'] == {'flush': 'succeeded', 'shutdown': 'succeeded'}

    built = read_json('go-build.json')
    assert [r['name'] for r in built['selection']['functions']] == [function]
    assert built['selection']['sha256'] == config['selection_sha256']
    for lane in ('plain', 'instrumented'):
        stream = gzip.GzipFile(fileobj=io.BytesIO(z.read(lane + '-image.gz')))
        image_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        assert image_sha == built[lane]['sha256'] == read_json(lane + '-go-plan.json')['binary']['sha256']
    wire = read_json('wire/forward-capture.json')
    assert wire['closed'] and wire['drained'] and wire['errors'] == [] and wire['unrecorded_events'] == 0
    counts = {'otelc.function.calls': [], 'otelc.function.duration': []}
    spans = set()
    event_paths = Counter()
    for index, event in enumerate(wire['events']):
        assert event['index'] == index and event['status'] == event['upstream']['status'] == 200
        assert event['upstream']['complete'] and not event['headers_write_failed']
        assert event['handler_finished_ns'] >= event['response_entity_written_ns'] >= event['headers_written_ns']
        entities = {}
        for role, record in (('request', event['request_entity']), ('upstream', event['upstream']['entity'])):
            assert record['file'] == f'{index:04d}-{role}.entity'
            raw = z.read('wire/' + record['file'])
            assert sha(raw) == record['sha256'] and len(raw) == record['size']
            entities[role] = raw
        event_paths[event['path']] += 1
        if event['path'] == '/v1/traces':
            assert ExportTraceServiceResponse.FromString(entities['upstream']).partial_success.rejected_spans == 0
            request = ExportTraceServiceRequest.FromString(entities['request'])
            for resource in request.resource_spans:
                assert attributes(resource.resource.attributes)['service.name'] == policy['resource']['service_name']
                for scope in resource.scope_spans:
                    for span in scope.spans:
                        assert span.name == function and span.end_time_unix_nano >= span.start_time_unix_nano
                        identity = (span.trace_id.hex(), span.span_id.hex())
                        assert identity not in spans
                        spans.add(identity)
            continue
        assert event['path'] == '/v1/metrics'
        assert ExportMetricsServiceResponse.FromString(entities['upstream']).partial_success.rejected_data_points == 0
        request = ExportMetricsServiceRequest.FromString(entities['request'])
        for resource in request.resource_metrics:
            if attributes(resource.resource.attributes).get('service.name') != policy['resource']['service_name']:
                continue
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name not in counts:
                        continue
                    data = metric.sum if metric.name.endswith('.calls') else metric.histogram
                    assert data.aggregation_temporality == 2
                    for point in data.data_points:
                        assert attributes(point.attributes) == {'function': function}
                        counts[metric.name].append(point.as_int if metric.name.endswith('.calls') else point.count)
    assert all(v and v == sorted(v) and v[-1] == expected_collected for v in counts.values())
    assert len(spans) == int(health['trace_exports']['exported_spans']) > 0
    assert health['trace_exports']['queue_pending'] == health['trace_exports']['queue_dropped'] == '0'
    assert int(health['trace_exports']['attempts']) == event_paths['/v1/traces']
    for filename, count_query in (('prometheus-counts-response.json', True), ('grafana-means-response.json', False)):
        response = read_json('downstream/' + filename)
        assert response['status'] == 'success' and len(response['data']['result']) == 5
        observed = {}
        for row in response['data']['result']:
            mode = row['metric']['mode']
            assert row['metric']['language'] == 'go' and mode not in observed
            observed[mode] = float(row['value'][1])
            if count_query:
                assert row['metric']['comparison_id'] == report['comparison_id'] and row['metric']['run_id'] == report['run_id']
        assert set(observed) == modes
        for mode in modes:
            expected = each if count_query else statistics.mean(timing[mode])
            assert math.isclose(observed[mode], expected, rel_tol=1e-9, abs_tol=.001)
    actual_dashboard = read_json('downstream/grafana-dashboard.json')['dashboard']
    with tarfile.open(fileobj=io.BytesIO(z.read('otelc-source.tar.gz')), mode='r:gz') as tar:
        expected_dashboard = json.load(tar.extractfile('deploy/grafana/dashboards/comparison.json'))
    a = next(p for p in actual_dashboard['panels'] if p['id'] == 2)
    b = next(p for p in expected_dashboard['panels'] if p['id'] == 2)
    assert a['targets'] == b['targets'] and a['datasource'] == b['datasource']
    result = {'status': 'pass for retained current-source Go benchmark observations',
        'run': run['id'], 'artifact': artifacts[0]['id'], 'zip_sha256': digest,
        'sdk_head': run['head_sha'], 'capture_head': capture_manifest['head'],
        'members_crc_checked': len(names), 'source_files_checked': source_counts,
        'samples': len(samples), 'samples_each_mode': each, 'invocations': expected_invocations,
        'collected_calls': expected_collected, 'decoded_spans': len(spans),
        'wire_requests': len(wire['events']), 'median_ns_per_operation': report['median_ns_per_operation'],
        'scope': 'Original recorded bytes, timings, telemetry and backend data only. No retained image execution, historical OS recertification, visual dashboard proof, whole-language admission or new platform/shipping claim.'}
    (folder / 'independent-audit.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
