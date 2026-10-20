"""Qualify the pinned MIR compiler and SDK with isolated execution profiles."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import build_rust_mir as builder
from .rust_coverage import line_coverage, llvm_environment, merge_line_reports
from .rust_llvm_coverage import source_hashes as shared_source_hashes


COMPONENTS = (
    "native/rust/mir/src/main.rs",
    "native/rust/mir/src/hooks.rs",
    "native/rust/mir/src/instrument.rs",
    "native/rust/frontend/src/definitions.rs",
    "crates/rust-probes/src/compiler_mir.rs",
    "crates/rust-probes/src/recording.rs",
    "crates/rust-probes/src/lib.rs",
    "crates/rust-probes/src/traces.rs",
)
EXPECTED_PAIRS = 8
IGNORE_SOURCES = ("/tests/|build.rs|/.cargo/registry/|/rustlib/src/|"
                  "/opt/homebrew/.*/include/|/Library/Developer/")


def source_hashes(root: Path) -> dict[str, str]:
    """Include compiler manifests, gate code and every maintained Rust input."""
    sources = shared_source_hashes(root)
    for name in ("native/rust/mir/Cargo.toml", "native/rust/mir/Cargo.lock",
                 "ci/build_rust_mir.py", "ci/rust_mir_coverage.py", "Makefile",
                 ".github/workflows/ci.yml"):
        sources[name] = builder.fingerprint(root / name)
    return sources


def environment() -> dict[str, str]:
    """Require the stable compiler and matching tools, without inherited probes."""
    if os.environ.get("RUSTC_BOOTSTRAP"):
        raise ValueError("MIR coverage requires RUSTC_BOOTSTRAP to be unset")
    values = {key: value for key, value in os.environ.items()
              if key not in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
                             "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS",
                             "CARGO_BUILD_TARGET", "CARGO_TARGET_DIR")
              and not key.startswith(("CARGO_LLVM_COV", "__CARGO_LLVM_COV",
                                      "OTEL_", "OTELC_"))}
    values["RUSTC"] = str(builder.qualified_rustc(
        values.get("RUSTC", "rustc"), values))
    if not builder.output(["cargo", "--version"], values).startswith("cargo 1.98.1 "):
        raise ValueError("MIR coverage requires Cargo 1.98.1")
    llvm_environment(values)
    for tool in ("LLVM_COV", "LLVM_PROFDATA"):
        if "LLVM version 22.1.8" not in builder.output([values[tool], "--version"], values):
            raise ValueError("MIR coverage requires matching LLVM 22.1.8 tools")
    return values


def command(arguments: list[str], root: Path, values: dict[str, str],
            log: Path) -> str:
    """Retain every build/execution channel and propagate failure."""
    with log.with_suffix(".stderr").open("wb") as errors:
        try:
            result = subprocess.run(arguments, cwd=root, env=values,
                                    stdout=subprocess.PIPE, stderr=errors,
                                    check=False, timeout=900)
        except subprocess.TimeoutExpired as error:
            log.write_bytes(error.stdout or b"")
            raise
    log.write_bytes(result.stdout)
    result.check_returncode()
    return result.stdout.decode()


def executable(output: str, name: str) -> Path:
    """Select the one Cargo test executable, never an arbitrary cached map."""
    records = [json.loads(line) for line in output.splitlines()]
    paths = [Path(record["executable"]) for record in records
             if record.get("reason") == "compiler-artifact"
             and record.get("target", {}).get("name") == name
             and record.get("executable")]
    if len(paths) != 1:
        raise ValueError(f"Expected exactly one {name} test executable")
    return paths[0]


def retain_group(folder: Path, binary: Path) -> Path:
    """Retain the exact map whose fresh raw profiles will be exported."""
    folder.mkdir(parents=True)
    retained = folder / "application"
    shutil.copy2(binary, retained)
    (folder / "profiles").mkdir()
    manifest = folder / "coverage.json"
    manifest.write_text(json.dumps({"schema_version": 1,
        "object": "application", "object_sha256": builder.fingerprint(retained),
        "profiles": "profiles"}, indent=2) + "\n")
    return manifest


def compile_sdk(root: Path, target: Path, run: Path, values: dict[str, str],
                stable: dict[str, str] | None) -> tuple[Path, Path]:
    """Execute fresh SDK tests, reusing only a real coverage build's cache."""
    sdk_values = dict(stable or values)
    if sdk_values.get("RUSTC_BOOTSTRAP"):
        raise ValueError("SDK coverage requires RUSTC_BOOTSTRAP to be unset")
    sdk_values.update(RUSTC=values["RUSTC"], RUSTFLAGS="-C instrument-coverage")
    sdk_values.pop("RUSTC_WRAPPER", None)
    sdk_values.pop("RUSTC_WORKSPACE_WRAPPER", None)
    sdk_values.pop("CARGO_ENCODED_RUSTFLAGS", None)
    sdk_values.setdefault("CARGO_TARGET_DIR", str(target / "sdk"))
    sdk_target = Path(sdk_values["CARGO_TARGET_DIR"])
    built = command(["cargo", "test", "-p", "quux-otelc-rust", "--lib",
                     "--locked", "--no-run", "--message-format=json"],
                    root, sdk_values, run / "sdk-build.log")
    group = retain_group(run / "sdk-unit", executable(built, "quux_otelc_rust"))
    sdk_values["LLVM_PROFILE_FILE"] = str(group.parent / "profiles/%p-%m.profraw")
    command([str(group.parent / "application"), "--test-threads=1"],
            root / "crates/rust-probes", sdk_values, run / "sdk-tests.log")
    command(["cargo", "build", "-p", "quux-otelc-rust", "--lib", "--locked"],
            root, sdk_values, run / "sdk-library.log")
    return sdk_target / "debug/libquux_otelc_rust.rlib", group


def compile_helper(root: Path, target: Path, run: Path,
                   values: dict[str, str]) -> tuple[Path, Path]:
    """Instrument only the helper; its private dependencies keep normal maps."""
    arguments = ["--manifest-path", str(root / "native/rust/mir/Cargo.toml"),
                 "--locked", "--target-dir", str(target / "helper")]
    build_values = dict(values, RUSTC_BOOTSTRAP="otelc_rust_mir")
    output = command(["cargo", "test", *arguments, "--test", "compiler",
                      "--no-run", "--message-format=json"], root, build_values,
                     run / "helper-tests-build.log")
    tests = executable(output, "compiler")
    library = Path(values["RUSTC"]).parent.parent / "lib"
    command(["cargo", "rustc", *arguments, "--bin", "otelc-rust-mir", "--",
             "-C", "instrument-coverage", "-C", "rpath",
             "-C", "link-arg=-Wl,-rpath," + str(library)], root, build_values,
            run / "helper-build.log")
    driver = target / "helper/debug/otelc-rust-mir"
    return tests, retain_group(run / "helper", driver)


def run_pairs(root: Path, run: Path, values: dict[str, str], sdk: Path,
              tests: Path, helper: Path) -> list[Path]:
    """Run the maintained compiler pairs against the current instrumented SDK."""
    driver = helper.parent / "application"
    profile = {"schema_version": 1, "backend": "compiler",
        "commit_hash": builder.RUST_COMMIT, "protected_source_roots":
        [str(root / "crates" / name) for name in ("config", "export", "rust-probes")]}
    for name, path in (("rustc", Path(values["RUSTC"])), ("sdk", sdk), ("driver", driver)):
        profile[name] = str(path)
        profile[name + "_sha256"] = builder.fingerprint(path)
    descriptor = run / "profile.json"
    descriptor.write_text(json.dumps(profile, indent=2) + "\n")
    applications = run / "applications"
    applications.mkdir()
    test_values = dict(values, OTELC_RUST_MIR_PROFILE=str(descriptor),
        OTELC_RUST_MIR_COVERAGE=str(applications),
        LLVM_PROFILE_FILE=str(helper.parent / "profiles/%p-%m.profraw"))
    command([str(tests), "--test-threads=1"], root, test_values,
            run / "compiler-tests.log")
    groups = sorted(applications.glob("*/instrumented/coverage.json"))
    if len(groups) != EXPECTED_PAIRS:
        raise ValueError(f"Expected {EXPECTED_PAIRS} executed compiler pairs; found {len(groups)}")
    return groups


def export_group(manifest: Path, values: dict[str, str]) -> str:
    """Never merge execution counts across different executable maps."""
    folder = manifest.parent.resolve()
    record = json.loads(manifest.read_text())
    binary = folder / record["object"]
    profiles = folder / record["profiles"]
    if (record.get("schema_version") != 1 or binary.is_symlink()
            or profiles.is_symlink() or not binary.resolve().is_relative_to(folder)
            or not profiles.resolve().is_relative_to(folder)
            or builder.fingerprint(binary) != record["object_sha256"]):
        raise ValueError("Coverage map identity or location differs")
    raw = sorted(profiles.glob("*.profraw"))
    if not raw or any(path.is_symlink() or not path.is_file() for path in raw):
        raise ValueError("Coverage group has no valid fresh execution profiles")
    merged = folder / "execution.profdata"
    subprocess.run([values["LLVM_PROFDATA"], "merge", "-sparse", *map(str, raw),
                    "-o", str(merged)], check=True, env=values)
    result = subprocess.run([values["LLVM_COV"], "export", str(binary),
        "--format=lcov", "--instr-profile=" + str(merged), "--fatal-warnings",
        "--ignore-filename-regex=" + IGNORE_SOURCES],
        capture_output=True, check=True, env=values)
    (folder / "execution.lcov").write_bytes(result.stdout)
    (folder / "execution.stderr").write_bytes(result.stderr)
    if result.stderr:
        raise ValueError("LLVM diagnostics invalidate the coverage export")
    line_coverage(result.stdout.decode())
    return result.stdout.decode()


def enforce_components(report: str) -> None:
    """Require fresh nonempty line maps for every compiler and SDK component."""
    for component in COMPONENTS:
        selected, active = [], False
        for line in report.splitlines():
            if line.startswith("SF:"):
                active = line.endswith("/" + component)
            if active:
                selected.append(line)
        percentage = line_coverage("\n".join(selected))
        print(f"{component}: {percentage:.2f}% (minimum 80%)", flush=True)
        if percentage < 80:
            raise ValueError(f"{component} MIR coverage is below 80%")


def run(root: Path | None = None,
        stable: dict[str, str] | None = None) -> str:
    """Publish a source-bound report only after fresh paired execution passes."""
    root = (root or Path.cwd()).resolve()
    target = root / "target/rust-mir-coverage"
    target.mkdir(parents=True, exist_ok=True)
    with (target / "qualification.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("MIR coverage is already running in this checkout") from None
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=target))
        output = root / "build/rust-mir-coverage.lcov"
        output.parent.mkdir(exist_ok=True)
        for old in (output, output.with_suffix(".json")):
            if old.exists():
                old.replace(run_dir / ("previous-" + old.name))
        before = source_hashes(root)
        values = environment()
        tools = {key: builder.fingerprint(Path(values[key]))
                 for key in ("RUSTC", "LLVM_COV", "LLVM_PROFDATA")}
        sdk, unit = compile_sdk(root, target, run_dir, values, stable)
        tests, helper = compile_helper(root, target, run_dir, values)
        sdk_hash = builder.fingerprint(sdk)
        applications = run_pairs(root, run_dir, values, sdk, tests, helper)
        report = merge_line_reports([export_group(path, values)
                                    for path in (unit, helper, *applications)])
        enforce_components(report)
        if source_hashes(root) != before:
            raise ValueError("MIR coverage sources changed during qualification")
        if (builder.fingerprint(sdk) != sdk_hash
                or any(builder.fingerprint(Path(values[key])) != digest
                       for key, digest in tools.items())):
            raise ValueError("MIR coverage compiler, SDK or LLVM tools changed")
        output.write_text(report)
        output.with_suffix(".json").write_text(json.dumps({
            "schema_version": 1, "source_root": str(root), "sources": before,
            "tools": {key: {"path": values[key], "sha256": digest}
                      for key, digest in tools.items()}, "sdk_sha256": sdk_hash,
            "report_sha256": builder.fingerprint(output), "evidence": str(run_dir),
            "groups": [str(path) for path in (unit, helper, *applications)],
            "compiler_pairs": len(applications)}, indent=2) + "\n")
        return report


if __name__ == "__main__":
    run()
