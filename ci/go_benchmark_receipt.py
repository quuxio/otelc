"""Check retained Go timings and actual downstream five-mode metric samples.

These joins validate the named benchmark observations. They do not establish
whole-program equivalence, historical kernel identity or oteleq admission.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Callable
from typing import Any

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest, ExportMetricsServiceResponse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from otelc import benchmark, benchmark_process
from otelc.config import Policy

MODES = ('plain', 'both_off', 'collection_only', 'emission_only', 'both_on')


def workload_selection(built: dict[str, Any]) -> str:
    """This fixed example must weave its workload before any timing is accepted."""
    selection = built['receipt']['selection']
    name = 'io.quux.otelc.examples/go.workload'
    require([row['name'] for row in selection['functions']] == [name]
            and not selection['types'] and not selection['allocations'] and not selection['releases'],
            'retained Go benchmark must select exactly the original workload function')
    return name


def instrumentation(value: dict[str, Any], health: dict[str, Any], configuration: dict[str, Any],
                    built: dict[str, Any]) -> dict[str, Any]:
    """Join actual woven invocation accounting, not just benchmark driver metrics."""
    name = workload_selection(built)
    require(configuration['functions'] == [name]
            and configuration['selection_sha256'] == built['receipt']['selection']['sha256']
            and configuration['policy_sha256'] == health['policy_sha256'] == value['config_sha256']
            and health['selection_sha256'] == configuration['selection_sha256']
            and health['pid'] == value['pids']['instrumented']
            and health['phase'] == 'final' and health['sdk_attached'] is True,
            'Go benchmark runtime configuration or final health differs from its woven selection')
    calls = 1 + (value['config']['benchmark']['warmup'] + 4 * value['config']['benchmark']['samples']) * value['iterations']
    probes = health['probe_accounting']
    require(probes['closed'] is True
            and probes['function_started'] == probes['function_ended'] == str(calls)
            and all(probes[key] == '0' for key in ('function_active', 'function_lost', 'unwind_unknown')),
            'Go benchmark did not execute and close every expected workload probe')
    return {'function': name, 'invocations': calls,
            'collected_calls': 2 * value['config']['benchmark']['samples'] * value['iterations']}


def workload_telemetry(capture: dict[str, Any], read: Callable[[str], bytes], *,
                       service: str, function: str, calls: int) -> dict[str, Any]:
    """Decode actual captured OTLP and require cumulative workload call/duration units."""
    observed: dict[str, list[int]] = {'otelc.function.calls': [], 'otelc.function.duration': []}
    for index, event in enumerate(capture['events']):
        require(event['index'] == index and event['upstream']['complete'] is True
                and event['upstream']['status'] == 200, 'incomplete Go benchmark telemetry exchange')
        entities = {}
        for role, field in (('request', event['request_entity']), ('upstream', event['upstream']['entity'])):
            expected = f'{index:04d}-{role}.entity'
            require(field['file'] == expected, 'Go benchmark telemetry entity name differs')
            raw = read(expected)
            require(len(raw) == field['size'] and hashlib.sha256(raw).hexdigest() == field['sha256'],
                    'Go benchmark telemetry entity bytes differ')
            entities[role] = raw
        if event['path'] == '/v1/traces':
            require(ExportTraceServiceResponse.FromString(entities['upstream']).partial_success.rejected_spans == 0,
                    'Collector rejected benchmark spans')
            continue
        require(event['path'] == '/v1/metrics', 'unexpected Go benchmark telemetry path')
        require(ExportMetricsServiceResponse.FromString(entities['upstream']).partial_success.rejected_data_points == 0,
                'Collector rejected benchmark metric points')
        request = ExportMetricsServiceRequest.FromString(entities['request'])
        for resource in request.resource_metrics:
            attributes = {row.key: row.value.string_value for row in resource.resource.attributes}
            if attributes.get('service.name') != service:
                continue
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name not in observed:
                        continue
                    data = metric.sum if metric.name.endswith('.calls') else metric.histogram
                    require(data.aggregation_temporality == 2, 'Go workload metrics must be cumulative')
                    for point in data.data_points:
                        labels = {row.key: row.value.string_value for row in point.attributes}
                        require(labels == {'function': function}, 'unexpected Go workload metric identity')
                        count = point.as_int if metric.name.endswith('.calls') else point.count
                        require(0 <= count <= calls, 'Go workload metric count exceeds collected batches')
                        observed[metric.name].append(count)
    require(all(counts and counts[-1] == calls and counts == sorted(counts) for counts in observed.values()),
            'actual Go workload call and duration telemetry is missing or incomplete')
    return {'function': function, 'collected_calls': calls,
            'observations': {name: len(counts) for name, counts in observed.items()}}


def require(condition: bool, message: str) -> None:
    """Reject incomplete observations instead of publishing a successful run."""
    if not condition:
        raise ValueError(message)


def integer(value: object, *, positive: bool = False) -> bool:
    """Booleans are not process IDs, checksums, timestamps or sample counts."""
    return type(value) is int and value >= (1 if positive else 0)


def same(left: object, right: object) -> bool:
    """Preserve primitive identity and refuse non-finite report values."""
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def usage(row: dict[str, Any], pid: int) -> None:
    """Check the CPU delta and RSS snapshot against their original observations."""
    before, after = row['before'], row['after']
    require(all(type(item['pid']) is int and item['pid'] == pid
                and integer(item['cpu_ns']) and integer(item['rss_bytes'], positive=True)
                and integer(item['observed_at_unix_ns'], positive=True)
                for item in (before, after)), 'Go benchmark process observation differs')
    require(after['observed_at_unix_ns'] >= before['observed_at_unix_ns']
            and integer(row['cpu_ns']) and row['cpu_ns'] == after['cpu_ns'] - before['cpu_ns']
            and integer(row['rss_bytes'], positive=True)
            and row['rss_bytes'] == max(before['rss_bytes'], after['rss_bytes']),
            'Go benchmark resource window differs from its snapshots')


def report(value: dict[str, Any], raw: list[dict], responses: list[dict],
           policy: Policy, comparison_id: str) -> dict[str, Any]:
    """Join original driver arrays, responses, state changes and process windows."""
    count, samples = policy.data['benchmark']['iterations'], policy.data['benchmark']['samples']
    expected = benchmark.checksum(count)
    require(type(value['schema_version']) is int and value['schema_version'] == 2
            and value['language'] == 'go' and value['workload'] == 'integer128-v1'
            and value['comparison_id'] == comparison_id and same(value['config'], policy.data)
            and value['source_complete'] is False and value['downstream_receipt'] == 'unavailable',
            'Go benchmark report differs from its caller-held run and policy')
    require(type(value['iterations']) is int and value['iterations'] == count
            and type(value['expected_checksum']) is int and value['expected_checksum'] == expected
            and set(value['samples_ns_per_operation']) == set(MODES),
            'Go benchmark workload or measured modes differ')
    pids, owners = value['pids'], value['supervisor_pids']
    require(set(pids) == set(owners) == {'plain', 'instrumented'}
            and all(integer(pid, positive=True) for pid in [*pids.values(), *owners.values()])
            and len(set([*pids.values(), *owners.values()])) == 4
            and type(value['sdk_pid']) is int and value['sdk_pid'] == pids['instrumented'],
            'Go benchmark requires original stock/woven owners and one embedded SDK')
    require(len(raw) == len(responses) == 5 * samples and same(value['process_samples'], raw),
            'Go benchmark raw sample denominator differs')
    order = [('plain', sample, 'plain') for sample in range(samples)]
    order += [('instrumented', sample, mode) for sample in range(samples)
              for mode in benchmark_process.state_order(sample, plain=False)]
    measured: dict[str, list[float]] = {mode: [] for mode in MODES}
    transitions = []
    for item, response, (lane, sample, mode) in zip(raw, responses, order, strict=True):
        require(type(item['sample']) is int and item['sample'] == sample
                and item['mode'] == mode and type(item['pid']) is int and item['pid'] == pids[lane]
                and type(item['calls']) is int and item['calls'] == count
                and type(item['checksum']) is int and item['checksum'] == expected
                and integer(item['elapsed_ns'], positive=True),
                'Go benchmark sample, checksum, PID or timing differs')
        original = {'calls': count, 'checksum': expected, 'elapsed_ns': item['elapsed_ns']}
        require(same(response, {'sample': sample, 'mode': mode, 'pid': pids[lane], 'response': original}),
                'Go benchmark retained application response differs')
        require(set(item['process_usage']) == {'application', 'supervisor'},
                'Go benchmark must count its embedded SDK exactly once')
        usage(item['process_usage']['application'], pids[lane])
        usage(item['process_usage']['supervisor'], owners[lane])
        measured[mode].append(item['elapsed_ns'] / count)
        if lane == 'instrumented':
            collection, emission = benchmark.STATES[mode]
            transitions.append({'sample': sample, 'mode': mode, 'schema_version': 1,
                'pid': pids[lane], 'collection': collection, 'emission': emission,
                'effective_emission_off': not emission})
    require(same(value['transitions'], transitions)
            and same(value['samples_ns_per_operation'], measured)
            and same(value['median_ns_per_operation'], {mode: statistics.median(rows) for mode, rows in measured.items()}),
            'Go benchmark controls or derived timing arrays differ')
    return {'language': 'go', 'comparison_id': comparison_id, 'run_id': value['run_id'],
            'workload': 'integer128-v1', 'modes': list(MODES), 'samples_each': samples,
            'expected_checksum': expected, 'pids': pids, 'supervisor_pids': owners,
            'scope': 'Retained application-body timings, state/PID joins and observed resource windows; not kernel recertification'}


def vector(document: dict[str, Any], value: dict[str, Any], *, counts: bool,
           aggregated: bool = False) -> dict[str, float]:
    """Require all current-run series and compare actual downstream aggregates."""
    require(document.get('status') == 'success' and 'warnings' not in document
            and 'infos' not in document and type(document.get('data')) is dict
            and document['data'].get('resultType') == 'vector'
            and type(document['data'].get('result')) is list,
            'benchmark metric query is not a complete instant vector')
    rows = document['data']['result']
    require(len(rows) == len(MODES), 'benchmark metric query lacks all five modes')
    result = {}
    for row in rows:
        labels = row['metric']
        mode = labels.get('mode')
        require(mode in MODES and mode not in result and labels.get('language') == 'go'
                and (not aggregated or not counts)
                and (aggregated or labels.get('comparison_id') == value['comparison_id']
                     and labels.get('run_id') == value['run_id'])
                and type(row['value']) is list and len(row['value']) == 2
                and type(row['value'][0]) in (int, float)
                and math.isfinite(row['value'][0]) and row['value'][0] > 0
                and type(row['value'][1]) is str,
                'benchmark metric query contains foreign, duplicate or stale series')
        number = float(row['value'][1])
        require(math.isfinite(number) and number > 0, 'benchmark metric sample is non-finite or nonpositive')
        wanted = (len(value['samples_ns_per_operation'][mode]) if counts
                  else statistics.mean(value['samples_ns_per_operation'][mode]))
        require(number == wanted if counts else math.isclose(number, wanted, rel_tol=1e-9, abs_tol=.001),
                'benchmark downstream count or mean differs from retained samples')
        result[mode] = number
    return result
