"""Paired original-source Node workloads with independent SDK-process accounting."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from otelc import benchmark, benchmark_process, control, node, node_provenance
from otelc.config import Policy


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
            if endpoint is not None:
                control.request(endpoint, 'flush')
            benchmark_process.measure_batches(process, policy, output, endpoint, sdk_pid, expected, records)
            if endpoint is not None:
                benchmark_process.finalize_metrics(process, endpoint, sdk_pid, output)
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
    compiler = 'none'
    if language == 'typescript':
        compiler = 'Microsoft native 7.0.2' if backend == 'native' else 'Microsoft API 6.0.3'
    identity = benchmark_process.RunIdentity(language, 'Node ' + manifest['runtime']['version'],
        compiler, ('source launch', '--enable-source-maps', compiler), before['sha256'])
    report = benchmark_process.summary(policy, output, records, identity, pids, sdk_pid, comparison_id)
    report['metric_finalization'] = node_provenance.identity(output / 'metric-finalization.json')
    report['config_sha256'] = hashlib.sha256(json.dumps(policy.data, sort_keys=True).encode()).hexdigest()
    report['launch_evidence'] = {lane: {name: node_provenance.identity(output / lane / (prefix + '-' + name + '.json'))
        for name in ('launch', 'inputs')} for lane, prefix in (('plain', 'node-plain'), ('instrumented', 'node'))}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    if publish:
        benchmark.export_result(report, policy.data['export']['endpoint'])
    return report
