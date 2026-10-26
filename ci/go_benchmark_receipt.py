"""Check retained Go timings and actual downstream five-mode metric samples.

These joins validate the named benchmark observations. They do not establish
whole-program equivalence, historical kernel identity or oteleq admission.
"""
from __future__ import annotations

import json
import math
import statistics
from typing import Any

from otelc import benchmark, benchmark_process
from otelc.config import Policy

MODES = ('plain', 'both_off', 'collection_only', 'emission_only', 'both_on')


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
