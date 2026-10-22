"""Paired original-source CPython workloads with a separately owned SDK process."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from otelc import benchmark, benchmark_process, control
from otelc import python as adapter
from otelc.config import Policy
from otelc.file_inputs import identity as file_identity
from otelc.python_sdk_process import SdkProcess, policy_digest


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
                ) -> Iterator[tuple[subprocess.Popen[str], SdkProcess | None]]:
    """Own the native app and SDK through final drain, including failed samples."""
    runtime.mkdir(mode=0o700)
    sdk = process = descriptor = None
    command = adapter.invocation(adapter.ROOT, entry=adapter.ROOT / 'examples/python/BenchmarkApp.py',
                                 module=None, arguments=())
    try:
        environment = None
        inherited = ()
        if not plain:
            sdk = SdkProcess.start(policy, runtime / 'sdk', bind=False)
            descriptor, environment = adapter.prepare(policy, runtime, sdk, adapter.ROOT)
            inherited = (*sdk.channel.descriptors, descriptor)
        with (runtime.parent / ('plain.stderr' if plain else 'instrumented.stderr')).open('w') as errors:
            process = subprocess.Popen(command, cwd=adapter.ROOT, env=environment, pass_fds=inherited,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
            (runtime / 'process.json').write_text(json.dumps({'driver_pid': os.getpid(),
                'application_pid': process.pid, 'sdk_pid': sdk.process.pid if sdk else None,
                'application_command': command}, indent=2) + '\n')
            if sdk is not None:
                sdk.channel.close()
                os.close(descriptor)
                descriptor = None
            try:
                yield process, sdk
            finally:
                try:
                    benchmark.stop(process)
                finally:
                    if sdk is not None:
                        report = sdk.wait_report(application_pid=process.pid)
                        validate_drain(report, process.pid, sdk.process.pid,
                                       lifetimes=policy.data['lifetimes']['enabled'])
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
        finally:
            if sdk is not None:
                sdk.abort()
        if process is not None:
            (runtime / 'exit.json').write_text(json.dumps({'application_exit': process.returncode,
                'sdk_exit': sdk.process.poll() if sdk else None}) + '\n')


def measure_process(policy: Policy, output: Path, lane: str,
                    expected: int, records: benchmark.Measurements) -> tuple[int, int | None]:
    """The driver launches the real app directly, without inventing a wrapper PID."""
    plain = lane == 'plain'
    runtime = output / lane
    endpoint = None if plain else runtime / 'control.json'
    with application(policy, runtime, plain=plain) as (process, sdk):
        sdk_pid = sdk.process.pid if sdk else None
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
        benchmark_process.measure_batches(process, policy, output, endpoint, sdk_pid, expected, records)
        return process.pid, sdk_pid


def run(policy: Policy, output: Path, *, comparison_id: str | None = None,
        publish: bool = True) -> dict[str, Any]:
    """Compare the unchanged seeded Python app in plain and four live modes."""
    adapter.validate_policy(policy)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = adapter.ROOT / 'examples/python/BenchmarkApp.py'
    before = file_identity(source)
    records = benchmark.Measurements()
    expected = benchmark.checksum(policy.data['benchmark']['iterations'])
    pids = {}
    sdk_pid = None
    for lane in ('plain', 'instrumented'):
        pids[lane], child = measure_process(policy, output, lane, expected, records)
        sdk_pid = child or sdk_pid
    if file_identity(source) != before:
        raise ValueError('application source changed')
    identity = benchmark_process.RunIdentity('python', platform.python_implementation() + ' ' + platform.python_version(),
        'CPython bytecode', ('native source launch', 'no additional interpreter flags'), before['sha256'])
    report = benchmark_process.summary(policy, output, records, identity, pids, sdk_pid, comparison_id)
    report['config_sha256'] = policy_digest(policy)
    executable = Path(sys.executable).resolve(strict=True)
    report['interpreter'] = {'path': sys.executable, 'resolved_path': str(executable),
        'sha256': hashlib.sha256(executable.read_bytes()).hexdigest(), 'version': sys.version,
        'environment': {key: os.environ.get(key) for key in ('PYTHONOPTIMIZE', 'PYTHONHASHSEED', 'PYTHONDONTWRITEBYTECODE')}}
    report['requirements_sha256'] = hashlib.sha256((adapter.ROOT / 'requirements.txt').read_bytes()).hexdigest()
    report['source_evidence_scope'] = 'internal example source hashes and bootstrap files; full Python import/code provenance unqualified'
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    if publish:
        benchmark.export_result(report, policy.data['export']['endpoint'])
    return report
