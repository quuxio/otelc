"""Qualify compiler hooks with LLVM 23 profiles, separately from stable LLVM 22."""
import fcntl
import json
import os
from pathlib import Path
import subprocess

from . import build_rust_llvm as builder
from .rust_coverage import line_coverage, merge_line_reports


COMPONENTS = ("crates/rust-adapter/src/compiler_inputs.rs",
              "crates/rust-adapter/src/compiler_profile.rs",
              "crates/rust-adapter/src/compiler_llvm.rs",
              "crates/rust-adapter/src/compiler_frontend.rs",
              "native/rust/frontend/src/main.rs",
              "native/rust/OtelcRustPass.cpp")


def source_hashes(root: Path) -> dict[str, str]:
    """Reject stale or concurrently changed source maps before combining reports."""
    files = [root / name for name in ("Cargo.toml", "Cargo.lock",
             "native/rust/frontend/Cargo.toml", "native/rust/frontend/Cargo.lock",
             "ci/build_rust_llvm.py", "ci/rust_llvm_coverage.py", "ci/rust_coverage.py",
             "tests/fixtures/trace-context.json")]
    for directory in ("crates", "native", "include"):
        files.extend(path for path in (root / directory).rglob("*")
                     if path.is_file() and path.suffix in (".rs", ".cpp", ".h", ".hpp")
                     and "target" not in path.relative_to(root).parts)
    return {str(path.relative_to(root)): builder.fingerprint(path)
            for path in sorted(files)}


def environment(root: Path, target: Path) -> tuple[dict[str, str], str]:
    """Use the project Cargo and exact profile compiler without coverage wrappers."""
    if not builder.output(["cargo", "--version"]).startswith("cargo 1.98.1 "):
        raise ValueError("Compiler coverage requires the project's Cargo 1.98.1")
    descriptor = root / "target/debug/otelc-rust-llvm.json"
    with descriptor.open("rb") as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ValueError("Rust compiler profile exceeds 64 KiB")
    rustc = builder.qualified_rustc(json.loads(data)["rustc"])
    values = {key: value for key, value in os.environ.items()
              if key not in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS",
                             "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_TARGET")
              and not key.startswith(("CARGO_LLVM_COV", "__CARGO_LLVM_COV"))}
    values.update(RUSTC=str(rustc), CARGO_TARGET_DIR=str(target),
                  RUSTFLAGS="-C instrument-coverage",
                  LLVM_PROFILE_FILE=str(target / "profiles/%p-%m.profraw"),
                  OTELC_COVERAGE_BIN_DIR=str(target / "retained"))
    config = values.get("OTELC_RUST_LLVM_CONFIG", "/opt/homebrew/opt/llvm@23/bin/llvm-config")
    return values, config


def export_report(target: Path, tools: Path, values: dict[str, str], plugin: Path) -> str:
    """Export every executed map independently with matching LLVM tools."""
    profiles = sorted(target.rglob("*.profraw"))
    if not profiles:
        raise ValueError("Rust compiler coverage produced no execution profiles")
    merged = target / "llvm23.profdata"
    subprocess.run([str(tools / "llvm-profdata"), "merge", "-sparse",
                    *map(str, profiles), "-o", str(merged)], check=True, env=values)
    objects = [path for path in (target / "debug/deps").iterdir()
               if path.is_file() and not path.suffix and os.access(path, os.X_OK)]
    objects.extend([target / "debug/otelc-rust-adapter", target / "debug/otelc-rust-frontend", plugin,
                    *sorted((target / "retained").glob("*"))])
    command = [str(tools / "llvm-cov"), "export", "--format=lcov",
               "--instr-profile=" + str(merged),
               "--ignore-filename-regex=/tests/|build.rs|/.cargo/registry/|/rustlib/src/|/opt/homebrew/.*/include/|/Library/Developer/|/\\.tmp[^/]+/"]
    report = merge_line_reports([subprocess.check_output(
        command + ["--object", str(path)], text=True, env=values) for path in objects])
    for component in COMPONENTS:
        selected, active = [], False
        for line in report.splitlines():
            if line.startswith("SF:"):
                active = line.endswith("/" + component)
            if active:
                selected.append(line)
        percentage = line_coverage("\n".join(selected))
        print(f"{component} line coverage: {percentage:.2f}% (minimum 80%)", flush=True)
        if percentage < 80:
            raise ValueError(f"{component} compiler coverage is below 80%")
    return report


def run(root: Path | None = None) -> str:
    """Build, execute and enforce a fresh compiler-profile qualification."""
    root = (root or Path.cwd()).resolve()
    target = root / "target/rust-llvm-coverage"
    target.mkdir(parents=True, exist_ok=True)
    with (target / "qualification.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Rust compiler coverage is already running in this checkout") from None
        before = source_hashes(root)
        values, config = environment(root, target)
        tools = builder.llvm_tools(config)
        builder.check_frontend(root, Path(values['RUSTC']), values)
        for profile in target.rglob("*.profraw"):
            profile.unlink()
        for binary in (target / "retained").glob("*"):
            binary.unlink()
        (target / "profiles").mkdir(exist_ok=True)
        subprocess.run(["cargo", "build", "-p", "quux-otelc-rust", "-p",
                        "quux-otelc-rust-adapter", "--locked"], cwd=root, env=values, check=True)
        destination = target / "debug"
        plugin = builder.compile_pass(root, destination, config, values, coverage=True)
        # The compiler-private workspace can instantiate dependency helpers with
        # different feature/codegen contexts. Give its coverage symbols a distinct
        # crate identity before combining raw counts with adapter/SDK profiles.
        # Retain otherwise stripped SDK ABI counters: their zero-count mappings
        # must not be mistaken for the differently compiled application's ABI.
        frontend_values = dict(values, RUSTFLAGS=values["RUSTFLAGS"]
                               + " -C metadata=otelc_frontend_coverage -C link-dead-code")
        frontend = builder.compile_frontend(root, destination, Path(values["RUSTC"]), frontend_values,
                                            target / "frontend")
        builder.write_profile(root, destination, Path(values["RUSTC"]),
                              destination / "libquux_otelc_rust.rlib", plugin, frontend)
        subprocess.run(["cargo", "test", "-p", "quux-otelc-rust-adapter", "--lib",
                        "--test", "compiler_inventory", "--test", "compiler_frontend", "--test", "compiler_llvm", "--locked",
                        "--", "--include-ignored", "--test-threads=1"],
                       cwd=root, env=values, check=True)
        report = export_report(target, tools, values, plugin)
        if source_hashes(root) != before:
            raise ValueError("Rust compiler coverage sources changed during qualification")
        output = root / "build/rust-llvm-coverage.lcov"
        output.parent.mkdir(exist_ok=True)
        output.write_text(report)
        output.with_suffix(".json").write_text(json.dumps({
            "schema_version": 1, "source_root": str(root), "sources": before,
            "report_sha256": builder.fingerprint(output)}, indent=2) + "\n")
        return report


if __name__ == "__main__":
    run()
