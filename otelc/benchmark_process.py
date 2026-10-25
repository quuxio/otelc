"""Shared process samples and live-state scheduling for source-free benchmarks."""

from __future__ import annotations

import json
import os
import platform
import re
import statistics
import subprocess
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from otelc import benchmark, control
from otelc.config import Policy


def cpu_time_ns(value: str) -> int:
    """Parse the macOS/Linux ps cumulative CPU-time formats without adding precision."""
    match = re.fullmatch(r'(?:(\d+)-)?(?:(\d+):)?(\d+):(\d{2}(?:\.\d+)?)', value)
    if not match or (match[2] is not None and int(match[3]) >= 60) or Decimal(match[4]) >= 60:
        raise ValueError('unsupported ps CPU-time value')
    seconds = int(match[1] or 0) * 86400 + int(match[2] or 0) * 3600 + int(match[3]) * 60 + Decimal(match[4])
    return int(seconds * 1_000_000_000)


def process_usage(pid: int) -> dict[str, int]:
    """Observe a live process externally; neither application nor SDK needs benchmark code."""
    value = subprocess.check_output(['ps', '-p', str(pid), '-o', 'pid=,time=,rss='], text=True, timeout=3).split()
    if len(value) != 3 or int(value[0]) != pid or int(value[2]) <= 0:
        raise ValueError('process CPU/RSS observation is unavailable')
    return {'pid': pid, 'cpu_ns': cpu_time_ns(value[1]), 'rss_bytes': int(value[2]) * 1024,
            'observed_at_unix_ns': time.time_ns()}


def measured_batch(process: benchmark.Application, count: int, endpoint: Path | None,
                   sdk_pid: int | None, output: Path, sample: int, mode: str, *,
                   supervisor_pid: int | None = None) -> dict[str, Any]:
    """Keep application-body latency separate from the CPU window including SDK flush."""
    pids = {'application': process.pid, **({'sdk': sdk_pid} if sdk_pid is not None else {})}
    if supervisor_pid is not None:
        pids['supervisor'] = supervisor_pid
    before = {role: process_usage(pid) for role, pid in pids.items()}
    result = benchmark.batch(process, count)
    with (output / 'responses.jsonl').open('a') as raw:
        raw.write(json.dumps({'response': result, 'sample': sample, 'mode': mode, 'pid': process.pid}) + '\n')
    if endpoint is not None:
        control.request(endpoint, 'flush')
    after = {role: process_usage(pid) for role, pid in pids.items()}
    usage = {}
    for role in pids:
        delta = after[role]['cpu_ns'] - before[role]['cpu_ns']
        if delta < 0:
            raise ValueError('process CPU counter decreased')
        usage[role] = {'cpu_ns': delta, 'rss_bytes': max(before[role]['rss_bytes'], after[role]['rss_bytes']),
                       'before': before[role], 'after': after[role]}
    return {**result, 'process_usage': usage}


def measure_batches(process: benchmark.Application, policy: Policy, output: Path, endpoint: Path | None,
                    sdk_pid: int | None, expected: int, records: benchmark.Measurements, *,
                    supervisor_pid: int | None = None) -> None:
    """Alternate live states, retaining each raw response before checksum acceptance."""
    configuration = policy.data['benchmark']
    for sample in range(configuration['samples']):
        for mode in state_order(sample, plain=endpoint is None):
            if endpoint is not None:
                state = benchmark.switch_state(endpoint, process, mode)
                if not sdk_identity(state, sdk_pid) or (state['collection'], state['emission']) != benchmark.STATES[mode]:
                    raise ValueError('benchmark live state or SDK process changed')
                records.transitions.append({'sample': sample, 'mode': mode, **state})
            result = measured_batch(process, configuration['iterations'], endpoint, sdk_pid,
                                    output, sample, mode, supervisor_pid=supervisor_pid)
            with (output / 'raw.jsonl').open('a') as raw:
                raw.write(json.dumps({**result, 'sample': sample, 'mode': mode, 'pid': process.pid}) + '\n')
            if result['checksum'] != expected:
                raise ValueError('plain/instrumented workload checksum differs')
            records.samples[mode].append(result['elapsed_ns'] / configuration['iterations'])


def state_order(sample: int, *, plain: bool) -> list[str]:
    """Balance state positions per block and reverse the following block."""
    modes = ['plain'] if plain else list(benchmark.STATES)
    offset = sample % len(modes)
    order = modes[offset:] + modes[:offset]
    if sample // len(modes) % 2:
        order.reverse()
    return order


def sdk_identity(state: dict[str, Any], sdk_pid: int | None) -> bool:
    """Embedded SDKs have no separate PID; external SDKs require an exact integer."""
    observed = state.get('sdk_pid')
    return observed is None if sdk_pid is None else type(observed) is int and observed == sdk_pid


def finalize_metrics(process: benchmark.Application, endpoint: Path, sdk_pid: int | None,
                     output: Path) -> None:
    """Emit retained collection-only values after the last timed sample."""
    receipt: dict[str, Any] = {'schema_version': 1}
    for label, command in (('restored', 'on'), ('flushed', 'flush')):
        state = control.request(endpoint, command)
        receipt[label] = state
        (output / 'metric-finalization.json').write_text(json.dumps(receipt, indent=2) + '\n')
        if (type(state.get('pid')) is not int or not sdk_identity(state, sdk_pid)
                or state['pid'] != process.pid
                or state['collection'] is not True or state['emission'] is not True):
            raise ValueError('benchmark final metric state or SDK process changed')


@dataclass(frozen=True)
class RunIdentity:
    """The language adapter supplies actual runtime and build identity."""

    language: str
    runtime: str
    compiler: str
    build_flags: tuple[str, ...]
    source_sha256: str


def summary(policy: Policy, output: Path, records: benchmark.Measurements,
            identity: RunIdentity, pids: dict[str, int], sdk_pid: int | None,
            comparison_id: str | None) -> dict[str, Any]:
    """Keep language adapters on one result and resource-window format."""
    run_id = uuid.uuid4().hex
    return {'schema_version': 2, 'run_id': run_id, 'comparison_id': comparison_id or run_id,
            'language': identity.language, 'runtime': identity.runtime, 'compiler': identity.compiler,
            'build_flags': list(identity.build_flags), 'host': platform.platform(),
            'machine': platform.machine(), 'workload': 'integer128-v1',
            'iterations': policy.data['benchmark']['iterations'], 'warmup': policy.data['benchmark']['warmup'],
            'expected_checksum': benchmark.checksum(policy.data['benchmark']['iterations']),
            'source_sha256': identity.source_sha256,
            'config': policy.data, 'pids': pids, 'sdk_pid': sdk_pid, 'transitions': records.transitions,
            'state_order_method': 'cyclic positions; reverse alternate complete blocks',
            'samples_ns_per_operation': records.samples,
            'median_ns_per_operation': {mode: statistics.median(values) for mode, values in records.samples.items()},
            'process_samples': [json.loads(line) for line in (output / 'raw.jsonl').read_text().splitlines()],
            'process_usage_method': {'source': 'ps cumulative CPU and RSS',
                'cpu_window': 'batch_and_sdk_flush',
                'rss': 'maximum of before/after snapshots; not peak allocation or total process peak',
                'cpu_precision': 'ps platform display resolution; zero can mean below resolution',
                'latency_window': 'application batch body only; external probes/evidence writes/flush are outside that timer'},
            'recorded_at_unix_ns': time.time_ns(), 'host_pid': os.getpid()}
