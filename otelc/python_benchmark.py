"""Paired prepared CPython workloads with actual application/SDK/owner evidence."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from otelc import benchmark, benchmark_process, control, python_benchmark_launch as launch
from otelc import python as adapter
from otelc.config import Policy
from otelc.file_inputs import identity as file_identity
from otelc.python_sdk_process import policy_digest


def counts(value: Any, names: set[str]) -> dict[str, int]:
    """Missing, boolean or unknown numeric accounting is not zero loss."""
    if (type(value) is not dict or not names <= set(value)
            or any(type(value[key]) is not int or value[key] < 0 for key in names)):
        raise ValueError('Python benchmark accounting is unavailable or invalid')
    return {key: value[key] for key in names}


def validate_drain(report: dict[str, Any], pid: int, sdk_pid: int, *, lifetimes: bool) -> None:
    """Require actual closed SDK evidence without claiming downstream receipt."""
    if (report.get('phase') != 'closed' or report.get('sdk_phase') != 'closed'
            or type(report.get('pid')) is not int or report['pid'] != pid
            or type(report.get('sdk_pid')) is not int or report['sdk_pid'] != sdk_pid
            or report.get('producer_checkpoint_available') is not True
            or report.get('shutdown_completed') is not True
            or report.get('shutdown_complete') is not True
            or report.get('shutdown_failed') is not False
            or report.get('operation_pending') is not False):
        raise ValueError('Python benchmark SDK drain is not verified')
    failures = counts(report, {'handoff_incomplete', 'protocol_errors', 'reply_lost',
        'operation_failures', 'export_failed', 'in_flight_exports', 'flush_failed', 'in_flight_flushes'})
    producer = report.get('producer')
    if type(producer) is not dict:
        raise ValueError('Python benchmark producer checkpoint is unavailable')
    failures.update(counts(producer, {'active_functions'}))
    losses = producer.get('runtime_losses')
    if type(losses) is not dict:
        raise ValueError('Python benchmark runtime loss accounting is unavailable')
    failures.update({'runtime.' + key: value for key, value in counts(losses, set(losses)).items()})
    handoff = counts(producer.get('handoff'), {'attempted', 'accepted', 'lost'})
    failures['handoff.lost'] = handoff['lost']
    if handoff['attempted'] != handoff['accepted']:
        raise ValueError('Python benchmark handoff accounting differs')
    for label in ('traces', 'metric_collection'):
        failures.update({label + '.' + key: value for key, value in
                         counts(report.get(label), {'failed', 'pending'}).items()})
    queue = counts(report.get('span_accounting'), {'received', 'sdk_exported', 'queue_pending', 'queue_dropped'})
    failures.update({key: queue[key] for key in ('queue_pending', 'queue_dropped')})
    observed = counts(producer.get('span_observation'), {'sampled_started', 'sampled_ended', 'handed_off'})
    if (report['span_accounting'].get('queue_evidence') != 'derived_after_shutdown'
            or len({*observed.values(), queue['received'], queue['sdk_exported']}) != 1):
        raise ValueError('Python benchmark span accounting is incomplete')
    if lifetimes:
        lifetime = producer.get('lifetimes')
        failures['lifetime.lost'] = counts(lifetime, {'lost'})['lost']
    if any(failures.values()):
        raise ValueError('Python benchmark has observation loss, failed exports or pending work')


@contextmanager
def application(policy: Policy, runtime: Path, *, plain: bool
                ) -> Iterator[tuple[launch.Application, int | None]]:
    """Validate the actual public launch and its independent final SDK drain."""
    with launch.application(policy, runtime, plain=plain) as process:
        yield process, process.sdk_pid
    if not plain:
        validate_drain(process.sdk_report, process.pid, process.sdk_pid,
                       lifetimes=policy.data['lifetimes']['enabled'])


def measure_process(policy: Policy, output: Path, lane: str,
                    expected: int, records: benchmark.Measurements) -> launch.Application:
    """Measure actual application/SDK identities separately from the supervisor."""
    plain = lane == 'plain'
    runtime = output / lane
    endpoint = None if plain else runtime / 'control.json'
    with application(policy, runtime, plain=plain) as (process, sdk_pid):
        if endpoint is not None:
            ready = control.wait_ready(endpoint, process.pid)
            if ready.get('sdk_pid') != sdk_pid:
                raise ValueError('Python benchmark SDK identity differs')
            control.request(endpoint, 'off')
        for _ in range(policy.data['benchmark']['warmup']):
            if benchmark.batch(process, policy.data['benchmark']['iterations'])['checksum'] != expected:
                raise ValueError('warmup checksum mismatch')
        if endpoint is not None:
            control.request(endpoint, 'flush')
        benchmark_process.measure_batches(process, policy, output, endpoint, sdk_pid, expected, records,
                                          supervisor_pid=process.supervisor_pid)
    return process


def run(policy: Policy, output: Path, *, comparison_id: str | None = None,
        publish: bool = True) -> dict[str, Any]:
    """Compare the unchanged seeded Python app in plain and four live modes."""
    adapter.validate_policy(policy)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = adapter.ROOT / 'examples/python/BenchmarkApp.py'
    before = file_identity(source)
    records = benchmark.Measurements()
    expected = benchmark.checksum(policy.data['benchmark']['iterations'])
    processes = {lane: measure_process(policy, output, lane, expected, records)
                 for lane in ('plain', 'instrumented')}
    for process in processes.values():
        launch.recheck(process)
    pids = {lane: process.pid for lane, process in processes.items()}
    supervisors = {lane: process.supervisor_pid for lane, process in processes.items()}
    sdk_pid = processes['instrumented'].sdk_pid
    if file_identity(source) != before:
        raise ValueError('application source changed')
    identity = benchmark_process.RunIdentity('python', platform.python_implementation() + ' ' + platform.python_version(),
        'CPython bytecode', ('prepared native entry journal', 'no additional application interpreter flags'), before['sha256'])
    report = benchmark_process.summary(policy, output, records, identity, pids, sdk_pid, comparison_id)
    report['launch_profile'] = 'prepared-native-v1'
    report['supervisor_pids'] = supervisors
    report['launch_evidence'] = {lane: process.evidence for lane, process in processes.items()}
    report['config_sha256'] = policy_digest(policy)
    executable = Path(sys.executable).resolve(strict=True)
    report['interpreter'] = {'path': sys.executable, 'resolved_path': str(executable),
        'sha256': hashlib.sha256(executable.read_bytes()).hexdigest(), 'version': sys.version,
        'environment': {key: os.environ.get(key) for key in ('PYTHONOPTIMIZE', 'PYTHONHASHSEED', 'PYTHONDONTWRITEBYTECODE')}}
    report['requirements_sha256'] = hashlib.sha256((adapter.ROOT / 'requirements.txt').read_bytes()).hexdigest()
    report['source_evidence_scope'] = 'prepared public supervisor, consumed native entry, post-drain inputs and SDK ownership; full Python import/code provenance unqualified'
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    if publish:
        benchmark.export_result(report, policy.data['export']['endpoint'])
    return report
