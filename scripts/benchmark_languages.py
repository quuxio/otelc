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


def rust_bundle(root: Path, output: Path, config: Path, source: Path,
                environment: dict[str, str], backend: str) -> tuple[str, list[str], list[str], dict]:
    """Use the actual instrumented compiler settings for the plain comparison."""
    folder = Path(tempfile.mkdtemp(prefix="rust-build-", dir=output))
    bundle = folder / "bundle"
    prefix = [str(root / "target/debug/quux-otelc"), "--config", str(config), "rust"]
    with (folder / "build.stdout").open("wb") as stdout, (folder / "build.stderr").open("wb") as stderr:
        subprocess.run([*prefix, "--build", str(source), "--output-dir", str(bundle)],
                       cwd=root, env=environment, check=True, stdout=stdout, stderr=stderr)
    metadata = json.loads((bundle / "build.json").read_text())
    schema = 2 if backend == "llvm" else 3
    if metadata["schema_version"] != schema or metadata.get("backend") != backend:
        raise ValueError(f"Rust benchmark requires a schema-{schema} {backend} bundle")
    return (metadata["compiler"]["program"], metadata["baseline_flags"],
            [*prefix, "--run", str(bundle)], {"directory": str(bundle), "metadata": metadata})


def run(root, output, iterations, runs, language="python", endpoint=None, rust_async=False, node_async=False, rust_closures=False, native_typescript=False, rust_llvm=False):
    if not 1 <= iterations <= 1000000 or not 2 <= runs <= 100:
        raise ValueError("iterations must be 1..1000000 and runs 2..100")
    if language not in ("python", "javascript", "typescript", "java", "go", "rust"):
        raise ValueError("language benchmark adapter is not implemented")
    if rust_async and language != "rust":
        raise ValueError("async Rust benchmark requires language=rust")
    if node_async and language not in ("javascript", "typescript"):
        raise ValueError("async Node benchmark requires language=javascript or typescript")
    if rust_closures and (language != "rust" or rust_async):
        raise ValueError("Rust closure benchmark requires language=rust without --rust-async")
    if native_typescript and language != "typescript":
        raise ValueError("native TypeScript benchmark requires language=typescript")
    if rust_llvm and (language != "rust" or rust_async):
        raise ValueError("Rust LLVM benchmark requires language=rust without --rust-async")
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "runtime.json"
    for path in (report_path, output / "report.json"):
        path.unlink(missing_ok=True)
    private = tempfile.TemporaryDirectory(prefix="otelc-benchmark-", dir="/tmp")
    socket = Path(private.name) / "metrics.sock"
    config = output / "policy.toml"
    extension, adapter, interpreter = ("py", "python", sys.executable) if language == "python" else ("mts" if language == "typescript" else "mjs", "ts" if language == "typescript" else "node", os.environ.get("OTELC_NODE", shutil.which("node") or "node"))
    source = root / f"examples/apps/{language}_{'async_' if node_async else ''}latency.{extension}"
    if language == "java":
        adapter, interpreter = "java", os.environ.get("OTELC_JAVA", shutil.which("java") or "java")
        source = root / "examples/apps/JavaLatency.java"
    elif language == "go":
        adapter, interpreter = "go", os.environ.get("OTELC_GO", shutil.which("go") or "go")
        source = root / "examples/apps/go_latency.go"
    elif language == "rust":
        adapter, interpreter = "rust", os.environ.get("OTELC_RUSTC", shutil.which("rustc") or "rustc")
        source = root / f"examples/apps/{'rust_async_latency' if rust_async else 'rust_closure_latency' if rust_closures else 'rust_latency'}.rs"
    original = source.read_bytes()
    config.write_text((root / f"examples/{language}.toml").read_text().replace(f"examples.apps.{language}_app.*", f"examples.apps.{language}_latency.process_order").replace('[resource]', '[metrics]\nenabled = false\n\n[runtime]\ncontrol_socket = ' + json.dumps(str(socket)) + '\n\n[resource]'))
    if language == "java":
        config.write_text(config.read_text().replace("examples.apps.JavaApp*.*", "examples.apps.JavaLatency.process_order(*)"))
    elif rust_closures:
        config.write_text(config.read_text().replace("examples.apps.rust_latency.process_order", "examples.apps.rust_closure_latency.main.<closure>@*"))
    elif rust_async:
        config.write_text(config.read_text().replace("examples.apps.rust_latency.process_order", "examples.apps.rust_async_latency.process_order"))
    elif node_async:
        config.write_text(config.read_text().replace(f"examples.apps.{language}_latency.process_order", f"examples.apps.{language}_async_latency.process_order"))
    if native_typescript:
        config.write_text(config.read_text().replace('backend = "source"', 'backend = "native"'))
    if rust_llvm:
        config.write_text(config.read_text().replace('backend = "compiler"', 'backend = "llvm"'))
    if endpoint:
        config.write_text(config.read_text().replace("http://127.0.0.1:4318", endpoint))
    cli = root / "target/debug/quux-otelc"
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "OTELC_"))}
    environment.update(OTELC_PYTHON=sys.executable, OTELC_REPORT_PATH=str(report_path), OTELC_ADAPTER_ROOT=str(root / "adapters"))
    if language == "go":
        environment["OTELC_GO"] = interpreter
    elif language == "rust":
        if not rust_llvm:
            environment["OTELC_RUSTC"] = interpreter
    elif language == "java":
        environment["OTELC_JAVA"] = interpreter
    elif language != "python":
        environment["OTELC_NODE"] = interpreter
    if native_typescript:
        environment["OTELC_TYPESCRIPT_BACKEND"] = "native"
    processes = []
    rust_build = None
    try:
        instrumented_command = [str(cli), "--config", str(config), adapter, str(source)]
        plain_command = [interpreter, str(source)] if language != "typescript" else [interpreter, "--import", str(root / "adapters/node/plain.mjs"), str(source)]
        if language == "go":
            plain_command = [interpreter, "run", str(source)]
        elif language == "rust":
            backend = "llvm" if rust_llvm else "compiler"
            interpreter, flags, instrumented_command, rust_build = rust_bundle(root, output, config, source, environment, backend)
            plain_binary = output / "plain-rust"
            subprocess.run([interpreter, *flags, str(source), "-o", str(plain_binary)], check=True, env=environment, cwd=root)
            plain_command = [str(plain_binary)]
        for command in (plain_command, instrumented_command):
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
        if telemetry["pid"] != application_pid:
            raise ValueError("Runtime report belongs to another process")
        complete = telemetry["export_finished"] and not telemetry["export_loss"] and not any(telemetry["losses"].values()) and telemetry["function_calls"] == 1000 + iterations * runs
        if not complete or source.read_bytes() != original:
            raise ValueError("Incomplete telemetry or modified source")
        toolchain = sys.version.split()[0] if language == "python" else subprocess.check_output([interpreter, "version" if language == "go" else "--version"], text=True).strip()
        report = {"language": language, "rust_async": rust_async, "node_async": node_async, "rust_closures": rust_closures, "native_typescript": native_typescript, "host": platform.platform(), "architecture": platform.machine(), "toolchain": toolchain, "iterations_per_batch": iterations, "runs": runs, "same_instrumented_pid": application_pid, "source_sha256": hashlib.sha256(original).hexdigest(), "complete_telemetry": complete, "runtime": telemetry, "summary": compare(samples, iterations), "samples": samples}
        if rust_build is not None:
            report.update(rust_llvm=rust_llvm, rust_build=rust_build)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
        private.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=["python", "javascript", "typescript", "java", "go", "rust"], required=True)
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path("build/benchmarks/python"))
    parser.add_argument("--endpoint")
    parser.add_argument("--rust-async", action="store_true", help="Measure Rust async first-poll-to-completion probes")
    parser.add_argument("--node-async", action="store_true", help="Measure faithful Node async Promise completion observation")
    parser.add_argument("--rust-closures", action="store_true", help="Measure externally selected synchronous Rust closure bodies")
    parser.add_argument("--native-typescript", action="store_true", help="Use the pinned native compiler for both TypeScript benchmark lanes")
    parser.add_argument("--rust-llvm", action="store_true", help="Use the experimental pinned Rust compiler backend and its recorded baseline flags")
    args = parser.parse_args()
    result = run(Path(__file__).resolve().parents[1], args.output, args.iterations, args.runs, args.language, args.endpoint, args.rust_async, args.node_async, args.rust_closures, args.native_typescript, args.rust_llvm)
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
