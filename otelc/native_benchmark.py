"""Equivalent C/C++ integer workloads with stock and four live metric states."""
from __future__ import annotations

import json
from pathlib import Path
import platform
import shutil
import time
from typing import Any

from otelc import benchmark, benchmark_cleanup, benchmark_process, control, health, native_build, native_launch
from otelc import native_benchmark_launch as launch, native_gate
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]


def counters(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, int]:
    """Unavailable native counters cannot stand in for a verified zero."""
    if any(type(row.get(key)) is not str or not row[key].isascii() or not row[key].isdigit() for key in keys):
        raise ValueError('native benchmark accounting is unavailable')
    return {key: int(row[key]) for key in keys}


def function_failures(value: dict[str, Any]) -> list[int]:
    """Every admitted function must end with available, loss-free accounting."""
    functions = counters(value['function_accounting'],
        ('started', 'ended', 'active', 'lost', 'invalid', 'metadata_errors'))
    if value['function_accounting']['closed'] is not True or functions['started'] != functions['ended']:
        raise ValueError('native benchmark function accounting is incomplete')
    return [functions[key] for key in ('active', 'lost', 'invalid', 'metadata_errors')]


def export_failures(exports: dict[str, Any]) -> list[int]:
    """SDK batch and unit completion must both explain every export attempt."""
    observed = counters(exports, ('batches_started', 'batches_succeeded', 'batches_failed',
                                'batches_pending', 'units_started', 'units_succeeded', 'units_failed'))
    if observed['batches_started'] != observed['batches_succeeded'] or observed['units_started'] != observed['units_succeeded']:
        raise ValueError('native benchmark exports are incomplete')
    return [observed[key] for key in ('batches_failed', 'batches_pending', 'units_failed')]


def lifetime_failures(value: dict[str, Any], enabled: bool) -> list[int]:
    """The declared C/C++ lifetime profile cannot hide pending or censored objects."""
    key = 'resource_accounting' if value['language'] == 'c' else 'object_accounting'
    pending = 'releasing' if value['language'] == 'c' else 'destroying'
    if (key in value) != enabled:
        raise ValueError('native benchmark lifetime profile differs')
    if not enabled:
        return []
    objects = value.get(key)
    if type(objects) is not dict or objects['closed'] is not True or objects['closing'] is not True:
        raise ValueError('native benchmark lifetime accounting is unavailable')
    observed = counters(objects, ('admitted', 'completed', 'live', pending, 'ending',
                                  'censored', 'lost', 'duplicates', 'invalid'))
    if observed['admitted'] != observed['completed']:
        raise ValueError('native benchmark has incomplete object lifetimes')
    return [observed[key] for key in ('live', pending, 'ending', 'censored', 'lost', 'duplicates', 'invalid')]


def validate_drain(report: dict[str, Any] | None, policy: Policy) -> None:
    """Require closed engines and complete SDK accounting before publishing timings."""
    value = health.validate(report)
    if (value['language'] not in ('c', 'cpp', 'rust') or value['phase'] != 'final' or value['sdk_attached'] is not True
            or value['lifecycle'] != {'flush': 'succeeded', 'shutdown': 'succeeded'}):
        raise ValueError('native benchmark SDK shutdown is incomplete')
    failures = [counters(value['metric_flush'], ('failed_attempts',))['failed_attempts']]
    failures.extend(function_failures(value))
    spans = counters(value['span_accounting'],
        ('sampled_started', 'sampled_ended', 'sampled_active', 'queue_pending', 'queue_dropped'))
    if value['span_accounting']['queue_evidence'] != 'derived_after_shutdown' or spans['sampled_started'] != spans['sampled_ended']:
        raise ValueError('native benchmark span accounting is incomplete')
    failures.extend(spans[key] for key in ('sampled_active', 'queue_pending', 'queue_dropped'))
    for kind in ('trace_exports', 'metric_exports'):
        failures.extend(export_failures(value[kind]))
    if spans['sampled_ended'] != int(value['trace_exports']['units_succeeded']):
        raise ValueError('native benchmark sampled spans differ from SDK exports')
    failures.extend(lifetime_failures(value, policy.data['lifetimes']['enabled']))
    if any(failures):
        raise ValueError('native benchmark has observation loss or failed exports')


def workload(policy: Policy, process: launch.Application, output: Path, endpoint: Path | None,
             records: benchmark.Measurements, expected: int) -> None:
    """Measure startup/warmup separately before sampling and final metric restore."""
    runtime = output / ('plain' if endpoint is None else 'instrumented')
    if endpoint is not None:
        control.wait_ready(endpoint, process.pid)
        control.request(endpoint, 'off')
    response = benchmark.batch(process, 1)
    process.first_response = {'elapsed_ns': time.monotonic_ns() - process.created_at_monotonic_ns,
        'calls': 1, 'response': response,
        'scope': 'process creation through first workload response; includes ownership/readiness, controls, IPC and body'}
    launch.prepared.files.write(runtime / 'native-benchmark-first-response.json', process.first_response)
    if response['checksum'] != benchmark.checksum(1):
        raise ValueError('native benchmark startup checksum differs')
    for _ in range(policy.data['benchmark']['warmup']):
        if benchmark.batch(process, policy.data['benchmark']['iterations'])['checksum'] != expected:
            raise ValueError('native benchmark warmup checksum differs')
    if endpoint is not None:
        control.request(endpoint, 'flush')
    benchmark_process.measure_batches(process, policy, output, endpoint, None, expected, records,
                                      supervisor_pid=process.supervisor_pid)
    if endpoint is not None:
        benchmark_process.finalize_metrics(process, endpoint, None, output)


def measure(policy: Policy, built: native_launch.Build, output: Path, *, plain: bool,
            records: benchmark.Measurements, expected: int) -> launch.Application:
    """Keep the startup handshake, warmup, body timer and resource windows separate."""
    runtime = output / ('plain' if plain else 'instrumented')
    endpoint = None if plain else runtime / 'app/control.json'
    process = None
    failure = None
    try:
        with launch.application(policy, built, runtime, plain=plain) as process:
            workload(policy, process, output, endpoint, records, expected)
    except BaseException as error:
        failure = error
        raise
    finally:
        if not plain and process is not None:
            if process.sdk_report is None and failure is not None:
                failure.add_note('Final native SDK report is unavailable; drain was not validated')
            else:
                cleanup = benchmark_cleanup.Cleanup(failure)
                cleanup.run(lambda: validate_drain(process.sdk_report, policy), 'Native benchmark final validation')
                cleanup.finish()
    launch.retain(process, runtime, plain=plain)
    return process


def run(policy: Policy, output: Path, *, language: str, sdk_directory: Path,
        comparison_id: str | None = None, publish: bool = True) -> dict[str, Any]:
    """Build both matched lanes without changing the maintained example source."""
    native_launch.validate_policy(policy, language, object_lifetimes=language == 'cpp', resource_lifetimes=language == 'c')
    source = ROOT / 'examples' / language / ('BenchmarkApp.c' if language == 'c' else 'BenchmarkApp.cpp')
    before = native_gate.identity(source)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output = output.resolve(strict=True)
    root = output / 'source'
    copied = root / source.relative_to(ROOT)
    copied.parent.mkdir(mode=0o700, parents=True)
    shutil.copyfile(source, copied)
    if native_gate.identity(copied)['sha256'] != before['sha256']:
        raise ValueError('native benchmark source copy differs')
    native_build.build(policy, language, root, [copied], output / 'compiled', sdk_directory=sdk_directory)
    built, _, manifest = native_build.verify(output / 'compiled', policy)
    records = benchmark.Measurements()
    expected = benchmark.checksum(policy.data['benchmark']['iterations'])
    processes = {lane: measure(policy, built, output, plain=lane == 'plain', records=records, expected=expected)
                 for lane in ('plain', 'instrumented')}
    for process in processes.values():
        launch.recheck(process)
    native_build.verify(output / 'compiled', policy)
    if native_gate.identity(source) != before:
        raise ValueError('native benchmark original source changed')
    tools = built.plugin.manifest()['toolchain']
    identity = benchmark_process.RunIdentity(language, 'native ' + platform.platform(), 'Clang ' + tools['version'],
        ('-O2', '-g', '-fexceptions', '-pthread', '-std=c11' if language == 'c' else '-std=c++17'), before['sha256'])
    report = benchmark_process.summary(policy, output, records, identity,
        {lane: process.pid for lane, process in processes.items()}, None, comparison_id)
    report.update(launch_profile='native-stock-plain-owned-v1', source_complete=False,
        config_sha256=native_launch.digest(policy.data),
        supervisor_pids={lane: process.supervisor_pid for lane, process in processes.items()},
        baseline={'plain': 'direct uninstrumented executable; no bootstrap, SDK, gate or supervisor',
                  'instrumented': 'owned application with embedded C++ SDK and separate supervisor'},
        launch_to_first_response={lane: process.first_response for lane, process in processes.items()},
        launch_evidence={lane: process.evidence for lane, process in processes.items()},
        original_source=before, native_build=manifest,
        sdk_health=processes['instrumented'].sdk_report,
        metric_finalization=native_gate.identity(output / 'metric-finalization.json'))
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    if publish:
        benchmark.export_result(report, policy.data['export']['endpoint'])
    return report
