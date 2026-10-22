"""Paired original-source Node workloads with independent SDK-process accounting."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import statistics
import subprocess
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from otelc import benchmark, control, node, node_provenance
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


def measured_batch(process: subprocess.Popen[str], count: int, endpoint: Path | None,
                   sdk_pid: int | None) -> dict[str, Any]:
    """Keep application-body latency separate from the CPU window including SDK flush."""
    pids = {'application': process.pid, **({'sdk': sdk_pid} if sdk_pid is not None else {})}
    before = {role: process_usage(pid) for role, pid in pids.items()}
    result = benchmark.batch(process, count)
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


def validate_drain(report: dict[str, Any], pid: int, sdk_pid: int) -> None:
    """Unknown or failed accounting cannot become a published benchmark."""
    if (report['phase'] != 'closed' or report['sdk_phase'] != 'closed'
            or report['pid'] != pid or report['sdk_pid'] != sdk_pid
            or report['producer_checkpoint_available'] is not True
            or report['span_accounting']['queue_evidence'] != 'derived_after_shutdown'):
        raise ValueError('Node benchmark SDK drain is not verified')
    zeros = [report['handoff']['lost'], report['handoff_incomplete'], report['lifetime_lost'],
             report['native_callback_failures'], report['active_functions'], report['export_failed'],
             report['in_flight_exports'], report['traces']['failed'], report['traces']['pending'],
             report['metric_collection']['failed'], report['metric_collection']['pending'],
             report['span_accounting']['queue_dropped'], report['span_accounting']['queue_pending'],
             *report['runtime_losses'].values()]
    if report['handoff']['sdk_process_failed'] is not False or any(type(value) is not int or value != 0 for value in zeros):
        raise ValueError('Node benchmark has observation loss, failed exports or unknown accounting')


def measure_process(policy: Policy, output: Path, runtime: Path, invocation: list[str], lane: str,
                    expected: int, records: benchmark.Measurements) -> tuple[int, int | None]:
    """Warm one original-source process and retain inputs through the independent drain."""
    plain = lane == 'plain'
    prefix = 'node-plain' if plain else 'node'
    manifest = json.loads((runtime / (prefix + '-launch.json')).read_text())
    configuration = policy.data['benchmark']
    endpoint = None if plain else runtime / 'control.json'
    sdk_pid = None
    with (output / (lane + '.stderr')).open('w') as errors:
        process = subprocess.Popen(invocation, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
        try:
            if endpoint is not None:
                ready = control.wait_ready(endpoint, process.pid)
                sdk_pid = ready['sdk_pid']
                if type(sdk_pid) is not int or sdk_pid <= 0 or sdk_pid == process.pid:
                    raise ValueError('independent SDK process identity is unavailable')
                control.request(endpoint, 'off')
            for _ in range(configuration['warmup']):
                if benchmark.batch(process, configuration['iterations'])['checksum'] != expected:
                    raise ValueError('warmup checksum mismatch')
            measure_batches(process, policy, output, endpoint, sdk_pid, expected, records)
        finally:
            finish_process(process, policy, runtime, manifest, sdk_pid)
    return process.pid, sdk_pid


def finish_process(process: subprocess.Popen[str], policy: Policy, runtime: Path,
                   manifest: dict[str, Any], sdk_pid: int | None) -> None:
    """Drain and retain verification even when an earlier sample fails."""
    try:
        try:
            benchmark.stop(process)
        finally:
            if sdk_pid is not None:
                final = node.wait_report(runtime, timeout=policy.data['export']['shutdown_timeout_ms'] / 1000 + 2.)
                validate_drain(final, process.pid, sdk_pid)
    finally:
        if not node_provenance.finish(runtime, manifest):
            raise ValueError('Node benchmark launch input verification failed')


def measure_batches(process: subprocess.Popen[str], policy: Policy, output: Path, endpoint: Path | None,
                    sdk_pid: int | None, expected: int, records: benchmark.Measurements) -> None:
    """Alternate live states, retaining each raw response before checksum acceptance."""
    configuration = policy.data['benchmark']
    order = ['plain'] if endpoint is None else list(benchmark.STATES)
    for sample in range(configuration['samples']):
        random.Random(sample).shuffle(order)
        for mode in order:
            if endpoint is not None:
                state = benchmark.switch_state(endpoint, process, mode)
                if state['sdk_pid'] != sdk_pid or (state['collection'], state['emission']) != benchmark.STATES[mode]:
                    raise ValueError('Node benchmark live state or SDK process changed')
                records.transitions.append({'sample': sample, 'mode': mode, **state})
            result = measured_batch(process, configuration['iterations'], endpoint, sdk_pid)
            with (output / 'raw.jsonl').open('a') as raw:
                raw.write(json.dumps({'sample': sample, 'mode': mode, 'pid': process.pid, **result}) + '\n')
            if result['checksum'] != expected:
                raise ValueError('plain/instrumented workload checksum differs')
            records.samples[mode].append(result['elapsed_ns'] / configuration['iterations'])


def run(policy: Policy, output: Path, *, language: str, backend: str = 'native',
        comparison_id: str | None = None, publish: bool = True) -> dict[str, Any]:
    """Compare equivalent JS/TS examples; retain raw evidence before driver publication."""
    if language not in ('javascript', 'typescript'):
        raise ValueError('Node benchmark requires javascript or typescript')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    extension = 'ts' if language == 'typescript' else 'js'
    source = node.ROOT / 'examples' / language / ('BenchmarkApp.' + extension)
    before = node_provenance.identity(source)
    records = benchmark.Measurements()
    expected = benchmark.checksum(policy.data['benchmark']['iterations'])
    pids = {}
    sdk_pid = None
    for plain in (True, False):
        lane = 'plain' if plain else 'instrumented'
        runtime = output / lane
        invocation = node.command(policy, runtime, language, backend=backend, plain=plain)
        pids[lane], child_pid = measure_process(policy, output, runtime, invocation, lane, expected, records)
        sdk_pid = child_pid or sdk_pid
    if node_provenance.identity(source) != before:
        raise ValueError('application source changed')
    manifest = json.loads((output / 'instrumented/node-launch.json').read_text())
    run_id = uuid.uuid4().hex
    compiler = ('Microsoft native 7.0.2' if backend == 'native' else 'Microsoft API 6.0.3') if language == 'typescript' else 'none'
    report = {'schema_version': 2, 'run_id': run_id, 'comparison_id': comparison_id or run_id,
              'language': language, 'runtime': 'Node ' + manifest['runtime']['version'], 'compiler': compiler,
              'build_flags': ['source launch', '--enable-source-maps', compiler],
              'host': platform.platform(), 'machine': platform.machine(), 'workload': 'integer128-v1',
              'iterations': policy.data['benchmark']['iterations'], 'warmup': policy.data['benchmark']['warmup'],
              'expected_checksum': expected, 'source_sha256': before['sha256'],
              'config_sha256': hashlib.sha256(json.dumps(policy.data, sort_keys=True).encode()).hexdigest(),
              'config': policy.data, 'pids': pids, 'sdk_pid': sdk_pid, 'transitions': records.transitions,
              'launch_evidence': {lane: {name: node_provenance.identity(output / lane / (prefix + '-' + name + '.json'))
                  for name in ('launch', 'inputs')} for lane, prefix in (('plain', 'node-plain'), ('instrumented', 'node'))},
              'samples_ns_per_operation': records.samples,
              'median_ns_per_operation': {mode: statistics.median(values) for mode, values in records.samples.items()},
              'process_samples': [json.loads(line) for line in (output / 'raw.jsonl').read_text().splitlines()],
              'process_usage_method': {'source': 'ps cumulative CPU and RSS', 'cpu_window': 'batch_and_sdk_flush',
                  'rss': 'maximum of before/after snapshots; not peak allocation or total process peak',
                  'cpu_precision': 'ps platform display resolution; zero can mean below resolution',
                  'latency_window': 'application batch body only; external probes/flush are outside that timer'},
              'recorded_at_unix_ns': time.time_ns(), 'host_pid': os.getpid()}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    if publish:
        benchmark.export_result(report, policy.data['export']['endpoint'])
    return report
