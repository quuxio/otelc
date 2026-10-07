"""Paired internal workload benchmark with explicit loss and result evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import time


def parse_output(output):
    match = re.search(r"elapsed_ns=(\d+) checksum=(\d+) calls=(\d+)", output)
    if not match or int(match[1]) <= 0:
        raise ValueError("Workload must report positive elapsed_ns, checksum and calls")
    return dict(zip(("elapsed_ns", "checksum", "calls"), map(int, match.groups())))


def measure(command, environment):
    begin = time.perf_counter_ns()
    result = subprocess.run(command, env=environment, text=True, capture_output=True, check=True)
    elapsed = time.perf_counter_ns() - begin
    result = parse_output(result.stdout)
    result["process_ns"] = elapsed
    return result


def benchmark(runs, iterations, threads, output):
    if not 2 <= runs <= 100 or not 1 <= iterations <= 10000000 or not 1 <= threads <= 16:
        raise ValueError("runs=2..100, iterations=1..10000000, threads=1..16")
    output.mkdir(parents=True, exist_ok=True)
    cli = Path("target/debug/quux-otelc").resolve()
    toolchain = json.loads(Path("target/debug/otelc-llvm-toolchain.json").read_text())
    compiler = str(Path(toolchain["bindir"]) / "clang++")
    source = Path("tests/fixtures/benchmark.cpp")
    flags = ["-O3", "-g", "-std=c++20", "-pthread"]
    baseline = (output / "baseline").resolve()
    instrumented = (output / "instrumented").resolve()
    config = (output / "otelc.toml").resolve()
    config.write_text('schema_version=1\n[build]\nbackend="llvm"\ninclude=["tests/fixtures/benchmark.cpp"]\n[functions]\ninclude=["selected_hot*"]\n[runtime]\nmax_threads=32\nstack_depth=64\nqueue_capacity=65536\n[export]\nendpoint="http://127.0.0.1:4318"\ninterval_ms=60000\n[resource]\nservice_name="otelc-benchmark"\n')
    subprocess.run([compiler, *flags, str(source), "-o", str(baseline)], check=True)
    subprocess.run([str(cli), "--config", str(config), "clang++", *flags, str(source), "-o", str(instrumented)], check=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("OTEL_", "OTELC_"))}
    lanes = {"baseline": [], "probes_disabled": [], "metrics_enabled": []}
    checksums = set()
    for run in range(runs):
        # Alternate order to reduce monotonic thermal/background-load bias.
        order = list(lanes) if run % 2 == 0 else list(reversed(lanes))
        for lane in order:
            settings = environment.copy()
            if lane == "metrics_enabled":
                report = output / f"observations-{run}.json"
                report.unlink(missing_ok=True)
                settings.update(OTELC_CONFIG=str(config), OTELC_MANIFEST=str(instrumented)+".otelc.json", OTELC_REPORT_PATH=str(report.resolve()))
            command = [str(baseline if lane == "baseline" else instrumented), str(iterations), str(threads)]
            measured = measure(command, settings)
            checksums.add(measured["checksum"])
            if lane == "metrics_enabled":
                measured["observations"] = json.loads(report.read_text())
                telemetry = measured["observations"]
                measured["complete_telemetry"] = telemetry["drained"] and telemetry["export_finished"] and not telemetry["export_dropped_batches"] and not any(telemetry["losses"].values()) and telemetry["function_calls"] == measured["calls"]
            lanes[lane].append(measured)
    if len(checksums) != 1:
        raise ValueError("Application checksums differ; benchmark is invalid")
    baseline_median = statistics.median(item["elapsed_ns"] for item in lanes["baseline"])
    summary = {}
    for lane, samples in lanes.items():
        values = [item["elapsed_ns"] for item in samples]
        median = statistics.median(values)
        summary[lane] = {"workload_median_ns": median, "workload_min_ns": min(values), "workload_max_ns": max(values), "workload_stdev_ns": statistics.stdev(values), "process_median_ns": statistics.median(item["process_ns"] for item in samples), "change_percent": 100 * (median / baseline_median - 1), "extra_ns_per_call": (median - baseline_median) / (iterations * threads)}
    report = {"host": platform.platform(), "architecture": platform.machine(), "compiler": subprocess.check_output([compiler, "--version"], text=True).splitlines()[0], "flags": flags, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "iterations_per_thread": iterations, "threads": threads, "runs": runs, "binary_bytes": {"baseline": baseline.stat().st_size, "instrumented": instrumented.stat().st_size}, "checksum_verified": True, "complete_telemetry": all(sample["complete_telemetry"] for sample in lanes["metrics_enabled"]), "summary": summary, "samples": lanes}
    (output / "results.json").write_text(json.dumps(report, indent=2)+"\n")
    text = ["# Internal instrumentation benchmark", "", f"Compiler: {report['compiler']}", f"Threads: {threads}; iterations/thread: {iterations}; repetitions: {runs}.", "", "| Lane | Workload median ms | Change | Extra ns/call | Whole process median ms |", "| --- | ---: | ---: | ---: | ---: |"]
    for lane, row in summary.items():
        text.append(f"| {lane} | {row['workload_median_ns']/1e6:.3f} | {row['change_percent']:+.1f}% | {row['extra_ns_per_call']:.1f} | {row['process_median_ns']/1e6:.3f} |")
    text.extend(["", f"Checksums match. Complete telemetry in every active run: {report['complete_telemetry']}.", "Loss counts and export failures are retained per run in results.json. Workload time excludes process startup and final export; whole-process time includes them. This measures this fixture on this host, not a general performance guarantee."])
    (output / "results.md").write_text("\n".join(text)+"\n")
    print("\n".join(text))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--iterations", type=int, default=100000)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("build/benchmarks"))
    arguments = parser.parse_args()
    benchmark(arguments.runs, arguments.iterations, arguments.threads, arguments.output)


if __name__ == "__main__":
    main()
