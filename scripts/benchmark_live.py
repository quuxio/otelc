"""Compare plain execution and live metrics off/on in one instrumented process."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import statistics
import subprocess
import platform


def read_line(process):
    """Require a prompt response rather than hanging on a failed demonstration."""
    if not select.select([process.stdout], [], [], 10)[0]:
        raise ValueError("Application response timed out")
    line = process.stdout.readline().strip()
    if not line:
        raise ValueError("Application exited before responding")
    return line


def batch(process, iterations):
    """Measure application work, excluding control requests and pipe round trips."""
    process.stdin.write(f"batch {iterations}\n")
    process.stdin.flush()
    values = dict(part.split("=", 1) for part in read_line(process).split())
    result = {key: int(values[key]) for key in ("elapsed_ns", "checksum", "calls")}
    if result["calls"] != iterations or result["elapsed_ns"] <= 0:
        raise ValueError("Invalid application measurement")
    return result


def control(cli, socket, command, pid):
    """Check that every toggle addresses the same live instrumented process."""
    result = json.loads(subprocess.check_output([str(cli), command, "--socket", str(socket)], text=True))
    if result["pid"] != pid:
        raise ValueError("Control socket belongs to another process")
    return result


def compare(samples, iterations):
    """Report incremental collection cost separately from compiled probe cost."""
    medians = {name: statistics.median(row["elapsed_ns"] for row in rows) for name, rows in samples.items()}
    return {"median_elapsed_ns": medians,
            "metrics_added_ns_per_call": (medians["metrics_on"] - medians["metrics_off"]) / iterations,
            "metrics_change_percent": 100 * (medians["metrics_on"] / medians["metrics_off"] - 1),
            "disabled_probe_ns_per_call": (medians["metrics_off"] - medians["baseline"]) / iterations,
            "total_instrumentation_ns_per_call": (medians["metrics_on"] - medians["baseline"]) / iterations}


def measure(cli, socket, plain, instrumented, iterations, runs, application_pid=None):
    """Alternate off/on ordering and keep both applications alive for every sample."""
    samples = {name: [] for name in ("baseline", "metrics_off", "metrics_on")}
    expected = None
    for index in range(runs):
        reference = batch(plain, iterations)
        samples["baseline"].append(reference)
        if expected is None:
            expected = reference["checksum"]
        phases = ("metrics_off", "metrics_on") if index % 2 == 0 else ("metrics_on", "metrics_off")
        for phase in phases:
            enabled = phase == "metrics_on"
            state = control(cli, socket, "enable" if enabled else "disable", application_pid or instrumented.pid)
            if state["metrics_enabled"] != enabled:
                raise ValueError("Metrics toggle was not applied")
            result = batch(instrumented, iterations)
            if result["checksum"] != expected or reference["checksum"] != expected:
                raise ValueError("Plain/instrumented results differ")
            samples[phase].append(result)
    return samples


def run(root, output, iterations, runs):
    """Build unchanged input, run paired measurements and validate shutdown evidence."""
    if not 1 <= iterations <= 1000000 or not 2 <= runs <= 100:
        raise ValueError("iterations must be 1..1000000 and runs 2..100")
    root = Path(root).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    private = output / "control"
    private.mkdir(mode=0o700, exist_ok=True)
    socket = private / "metrics.sock"
    cli = root / "target/debug/quux-otelc"
    toolchain = json.loads((root / "target/debug/otelc-llvm-toolchain.json").read_text())
    compiler = str(Path(toolchain["bindir"]) / "clang++")
    source = root / "examples/apps/live-latency.cpp"
    original = source.read_bytes()
    config = output / "live.toml"
    config.write_text((root / "examples/live.toml").read_text().replace("build/control/metrics.sock", str(socket)))
    plain_path, instrumented_path = output / "plain", output / "instrumented"
    flags = ["-O2", "-g", "-std=c++20"]
    subprocess.run([compiler, *flags, str(source), "-o", str(plain_path)], check=True, cwd=root)
    subprocess.run([str(cli), "--config", str(config), "clang++", *flags, str(source), "-o", str(instrumented_path)], check=True, cwd=root)
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("OTEL_", "OTELC_"))}
    report_path = output / "runtime.json"
    processes = []
    try:
        for command, env in [([str(plain_path)], environment),
                             ([str(cli), "--config", str(config), "--language", "cpp", "run", str(instrumented_path)], {**environment, "OTELC_REPORT_PATH": str(report_path)})]:
            process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
            processes.append(process)
            if read_line(process) != "ready":
                raise ValueError("Application did not initialise")
        plain, instrumented = processes
        batch(plain, 1000)
        batch(instrumented, 1000)
        control(cli, socket, "enable", instrumented.pid)
        batch(instrumented, 1000)
        samples = measure(cli, socket, plain, instrumented, iterations, runs)
        for process in processes:
            process.stdin.write("quit\n")
            process.stdin.flush()
            if process.wait(timeout=10) != 0:
                raise ValueError("Application failed")
        telemetry = json.loads(report_path.read_text())
        complete = telemetry["drained"] and telemetry["export_finished"] and not telemetry["export_dropped_batches"] and not any(telemetry["losses"].values()) and telemetry["function_calls"] == 1000 + iterations * runs
        if not complete or source.read_bytes() != original:
            raise ValueError("Incomplete telemetry or modified source")
        report = {"host": platform.platform(), "architecture": platform.machine(), "iterations_per_batch": iterations, "runs": runs, "same_instrumented_pid": instrumented.pid,
                  "source_sha256": hashlib.sha256(original).hexdigest(), "flags": flags,
                  "compiler": subprocess.check_output([compiler, "--version"], text=True).splitlines()[0],
                  "complete_telemetry": complete, "runtime": telemetry, "summary": compare(samples, iterations), "samples": samples}
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()


def main():
    """Expose the same repeatable measurement used by the developer guide."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=50000)
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path("build/benchmarks/live"))
    args = parser.parse_args()
    result = run(Path(__file__).resolve().parents[1], args.output, args.iterations, args.runs)
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
