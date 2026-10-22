"""Paired, same-process measurements and official-SDK benchmark export."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import select
import statistics
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otelc import control, java
from otelc.config import Policy

STATES = {"both_off": (False, False), "collection_only": (True, False),
          "emission_only": (False, True), "both_on": (True, True)}


@dataclass
class Measurements:
    """Raw measurements and state transitions from this comparison."""

    samples: dict[str, list[float]] = field(default_factory=lambda: {"plain": [], **{mode: [] for mode in STATES}})
    transitions: list[dict[str, Any]] = field(default_factory=list)


def checksum(count: int) -> int:
    """Independent workload oracle, outside the measured application."""
    total = 0
    for seed in range(count):
        value = seed
        for _ in range(128):
            value = (value * 1664525 + 1013904223) & 65535
            total += value
    return total


def batch(process: subprocess.Popen[str], count: int) -> dict[str, Any]:
    """Measure the application's batch body, excluding pipe/control overhead."""
    if process.stdin is None or process.stdout is None:
        raise ValueError("benchmark requires application pipes")
    process.stdin.write(f"{count}\n")
    process.stdin.flush()
    ready, _, _ = select.select([process.stdout], [], [], 30)
    if not ready:
        raise TimeoutError("application batch did not complete")
    value = json.loads(process.stdout.readline())
    if value.get("calls") != count or type(value.get("elapsed_ns")) is not int or value["elapsed_ns"] <= 0:
        raise ValueError("invalid application timing response")
    return value


def stop(process: subprocess.Popen[str]) -> None:
    """Bound shutdown and surface an unexpected application exit."""
    try:
        if process.poll() is None and process.stdin:
            process.stdin.write("quit\n")
            process.stdin.flush()
        if process.wait(timeout=10) != 0:
            raise ValueError("benchmark application exited unsuccessfully")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


def export_result(report: dict[str, Any], endpoint: str) -> None:
    """Send driver measurements through the official Python OTLP SDK."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricExportResult
    from opentelemetry.sdk.resources import Resource

    exporter = OTLPMetricExporter(endpoint=endpoint.rstrip("/") + "/v1/metrics", timeout=3)
    reader = InMemoryMetricReader()
    provider = MeterProvider(resource=Resource.create({"service.name": "otelc-benchmark-driver"}), metric_readers=[reader])
    try:
        meter = provider.get_meter("io.quux.otelc.benchmark")
        comparison_id = report.get("comparison_id", report["run_id"])
        meter.create_gauge("otelc.benchmark.run.timestamp", unit="s").set(
            report["recorded_at_unix_ns"] / 1_000_000_000.,
            {"comparison_id": comparison_id, "language": report["language"]},
        )
        duration = meter.create_histogram("otelc.benchmark.operation.duration", unit="s")
        for mode, values in report["samples_ns_per_operation"].items():
            labels = {"language": report["language"], "mode": mode, "run_id": report["run_id"], "comparison_id": comparison_id, "runtime": report["runtime"], "workload": "integer128-v1", "lifetimes": report["config"]["lifetimes"]["enabled"], "build": "; ".join(report["build_flags"]), "compiler": report.get("compiler", "javac")}
            for value in values:
                duration.record(value / 1_000_000_000., labels)
        export_process_usage(meter, report, comparison_id)
        metrics = reader.get_metrics_data()
        if metrics is None or exporter.export(metrics) is not MetricExportResult.SUCCESS:
            raise ValueError("Collector did not accept benchmark metrics")
    finally:
        provider.shutdown(timeout_millis=5000)
        exporter.shutdown(timeout_millis=5000)


def export_process_usage(meter: Any, report: dict[str, Any], comparison_id: str) -> None:
    """Publish per-role CPU windows and RSS snapshots separately from application latency."""
    if 'process_samples' not in report:
        return
    cpu = meter.create_histogram('otelc.benchmark.process.cpu.duration', unit='s')
    rss = meter.create_histogram('otelc.benchmark.process.rss', unit='By')
    for sample in report['process_samples']:
        for role, usage in sample['process_usage'].items():
            labels = {'comparison_id': comparison_id, 'run_id': report['run_id'],
                      'language': report['language'], 'mode': sample['mode'], 'role': role,
                      'window': 'batch_and_sdk_flush', 'compiler': report['compiler'],
                      'runtime': report['runtime'], 'build': '; '.join(report['build_flags']),
                      'workload': report['workload'], 'lifetimes': report['config']['lifetimes']['enabled']}
            cpu.record(usage['cpu_ns'] / report['iterations'] / 1_000_000_000., labels)
            rss.record(usage['rss_bytes'], labels)


def switch_state(socket: Path, process: subprocess.Popen[str], mode: str) -> dict[str, Any]:
    """Apply a live state and wait for the export-off barrier before timing."""
    collection, emission = STATES[mode]
    control.request(socket, "collection " + ("on" if collection else "off"))
    state = control.request(socket, "emission " + ("on" if emission else "off"))
    deadline = time.monotonic() + 5
    while not emission and state["in_flight_exports"] and time.monotonic() < deadline:
        time.sleep(.01)
        state = control.request(socket, "status")
    if not emission and state["in_flight_exports"]:
        raise TimeoutError("old exports did not settle before timing")
    if state["pid"] != process.pid:
        raise ValueError("instrumented process changed during live comparison")
    return state


def measure_batches(process: subprocess.Popen[str], policy: Policy, output: Path,
                    socket: Path, *, plain: bool, expected: int,
                    records: Measurements) -> None:
    """Retain raw samples immediately; functional failures never become timings."""
    configuration = policy.data["benchmark"]
    count = configuration["iterations"]
    order = ["plain"] if plain else list(STATES)
    for sample in range(configuration["samples"]):
        if not plain:
            random.Random(sample).shuffle(order)
        for mode in order:
            if not plain:
                records.transitions.append({"sample": sample, "mode": mode, **switch_state(socket, process, mode)})
            result = batch(process, count)
            with (output / "raw.jsonl").open("a") as raw:
                raw.write(json.dumps({"sample": sample, "mode": mode, "pid": process.pid, **result}) + "\n")
            if result["checksum"] != expected:
                raise ValueError("plain/instrumented workload checksum differs")
            records.samples[mode].append(result["elapsed_ns"] / count)


def check_health(socket: Path, policy: Policy, output: Path) -> None:
    """Reject incomplete observer health before any benchmark publication."""
    control.request(socket, "on")
    control.request(socket, "flush")
    health = control.request(socket, "status")
    (output / "health.json").write_text(json.dumps(health, indent=2) + "\n")
    if policy.data["lifetimes"]["enabled"] and health["lifetime_lost"] != 0:
        raise ValueError("lifetime loss is non-zero or unavailable; raw timings retained but not published")


def measure_process(policy: Policy, output: Path, runtime: Path, *, plain: bool,
                    expected: int, records: Measurements) -> int:
    """Warm one process, measure its supported states and bound its shutdown."""
    configuration = policy.data["benchmark"]
    with (output / ("plain.stderr" if plain else "instrumented.stderr")).open("w") as errors:
        process = subprocess.Popen(java.command(policy, runtime, plain=plain), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=errors, text=True)
        try:
            socket = runtime / "control.sock"
            if not plain:
                control.wait_ready(socket, process.pid)
                control.request(socket, "off")
            for _ in range(configuration["warmup"]):
                if batch(process, configuration["iterations"])["checksum"] != expected:
                    raise ValueError("warmup checksum mismatch")
            measure_batches(process, policy, output, socket, plain=plain, expected=expected,
                            records=records)
            if not plain:
                check_health(socket, policy, output)
            return process.pid
        finally:
            stop(process)


def run(policy: Policy, output: Path, *, publish: bool = True,
        comparison_id: str | None = None) -> dict[str, Any]:
    """Compare plain and four live states of one unchanged Java application."""
    configuration = policy.data["benchmark"]
    count = configuration["iterations"]
    expected = checksum(count)
    run_id = uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    source = java.ROOT / "examples/java/BenchmarkApp.java"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    records = Measurements()
    pids = {}
    with tempfile.TemporaryDirectory(prefix="otelc-bench-", dir="/tmp") as temporary:
        for plain in (True, False):
            pids["plain" if plain else "instrumented"] = measure_process(
                policy, output, Path(temporary), plain=plain, expected=expected,
                records=records)
    after = hashlib.sha256(source.read_bytes()).hexdigest()
    if before != after:
        raise ValueError("application source changed")
    version = subprocess.check_output([java.executable("java"), "-version"], stderr=subprocess.STDOUT, text=True).splitlines()[0]
    report = {"schema_version": 1, "run_id": run_id, "comparison_id": comparison_id or run_id, "language": "java", "runtime": version,
              "javaagent_version": java.AGENT_VERSION, "javaagent_sha256": java.AGENT_SHA256,
              "extension_sha256": hashlib.sha256((java.ROOT / "adapters/java/target/java-extension-0.2.0-SNAPSHOT.jar").read_bytes()).hexdigest(),
              "config_sha256": hashlib.sha256(json.dumps(policy.data, sort_keys=True).encode()).hexdigest(),
              "build_flags": ["javac default", "JVM default"],
              "host": platform.platform(), "machine": platform.machine(), "workload": "integer128-v1", "iterations": count,
              "warmup": configuration["warmup"], "expected_checksum": expected, "source_sha256": before,
              "config": policy.data, "pids": pids, "transitions": records.transitions,
              "samples_ns_per_operation": records.samples,
              "median_ns_per_operation": {mode: statistics.median(values) for mode, values in records.samples.items()},
              "recorded_at_unix_ns": time.time_ns(), "host_pid": os.getpid()}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    if publish:
        export_result(report, policy.data["export"]["endpoint"])
    return report
