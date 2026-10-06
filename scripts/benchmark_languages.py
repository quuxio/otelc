"""Compare unchanged language apps with same-process live metrics off/on."""
import argparse
import hashlib
import json
import os
import platform
import signal
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from scripts.benchmark_live import batch, compare, measure, read_line
except ModuleNotFoundError:
    from benchmark_live import batch, compare, measure, read_line


def run(root, output, iterations, runs, language="python", endpoint=None):
    if not 1 <= iterations <= 1000000 or not 2 <= runs <= 100:
        raise ValueError("iterations must be 1..1000000 and runs 2..100")
    if language not in ("python", "javascript"):
        raise ValueError("language benchmark adapter is not implemented")
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix="control-", dir=output))
    socket = private / "metrics.sock"
    config = output / "policy.toml"
    extension, adapter, interpreter = ("py", "python", sys.executable) if language == "python" else ("mjs", "node", os.environ.get("OTELC_NODE", shutil.which("node") or "node"))
    source = root / f"examples/apps/{language}_latency.{extension}"
    original = source.read_bytes()
    config.write_text((root / f"examples/{language}.toml").read_text().replace(f"examples.apps.{language}_app.*", f"examples.apps.{language}_latency.process_order").replace('[resource]', '[metrics]\nenabled = false\n\n[runtime]\ncontrol_socket = ' + json.dumps(str(socket)) + '\n\n[resource]'))
    if endpoint:
        config.write_text(config.read_text().replace("http://127.0.0.1:4318", endpoint))
    cli = root / "target/debug/quux-otelc"
    report_path = output / "runtime.json"
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "OTELC_"))}
    environment.update(OTELC_PYTHON=sys.executable, OTELC_REPORT_PATH=str(report_path))
    if language == "javascript":
        environment["OTELC_NODE"] = interpreter
    processes = []
    try:
        for command in ([interpreter, str(source)], [str(cli), "--config", str(config), adapter, str(source)]):
            process = subprocess.Popen(command, cwd=root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
            processes.append(process)
            if read_line(process) != "ready":
                raise ValueError("Application did not initialise")
        plain, instrumented = processes
        status = json.loads(subprocess.check_output([str(cli), "enable", "--socket", str(socket)], text=True))
        application_pid = status["pid"]
        batch(plain, 1000)
        batch(instrumented, 1000)
        samples = measure(cli, socket, plain, instrumented, iterations, runs, application_pid)
        for process in processes:
            process.stdin.write("quit\n")
            process.stdin.flush()
            if process.wait(timeout=10) != 0:
                raise ValueError("Application failed")
        telemetry = json.loads(report_path.read_text())
        complete = telemetry["export_finished"] and not telemetry["export_loss"] and not any(telemetry["losses"].values()) and telemetry["function_calls"] == 1000 + iterations * runs
        if not complete or source.read_bytes() != original:
            raise ValueError("Incomplete telemetry or modified source")
        toolchain = sys.version.split()[0] if language == "python" else subprocess.check_output([interpreter, "--version"], text=True).strip()
        report = {"language": language, "host": platform.platform(), "architecture": platform.machine(), "toolchain": toolchain, "iterations_per_batch": iterations, "runs": runs, "same_instrumented_pid": application_pid, "source_sha256": hashlib.sha256(original).hexdigest(), "complete_telemetry": complete, "runtime": telemetry, "summary": compare(samples, iterations), "samples": samples}
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
        shutil.rmtree(private)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=["python", "javascript"], required=True)
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path("build/benchmarks/python"))
    parser.add_argument("--endpoint")
    args = parser.parse_args()
    result = run(Path(__file__).resolve().parents[1], args.output, args.iterations, args.runs, args.language, args.endpoint)
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
