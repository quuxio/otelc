"""Enforce 80% coverage, including Rust and the shim inside native applications."""
import os
from pathlib import Path
import shlex
import subprocess


def llvm_environment(environment):
    """Locate tools matching rustc without installing or changing the toolchain."""
    if environment.get("LLVM_COV") and environment.get("LLVM_PROFDATA"):
        return
    version = subprocess.check_output(["rustc", "-vV"], text=True, env=environment)
    fields = dict(line.split(": ", 1) for line in version.splitlines() if ": " in line)
    major = int(fields["LLVM version"].split(".")[0])
    sysroot = subprocess.check_output(["rustc", "--print", "sysroot"], text=True, env=environment).strip()
    candidates = [Path(f"/opt/homebrew/opt/llvm@{major}/bin"), Path(sysroot) / "lib/rustlib" / fields["host"] / "bin"]
    for directory in candidates:
        if all((directory / name).is_file() for name in ("llvm-cov", "llvm-profdata", "clang")):
            environment.setdefault("LLVM_COV", str(directory / "llvm-cov"))
            environment.setdefault("LLVM_PROFDATA", str(directory / "llvm-profdata"))
            return
    raise ValueError("Install matching LLVM tools or set LLVM_COV and LLVM_PROFDATA")


def coverage_environment():
    """Use a separate build directory and parse exports without shell evaluation."""
    environment = os.environ.copy()
    llvm_environment(environment)
    environment["CARGO_TARGET_DIR"] = str(Path("target/coverage-build").resolve())
    settings = subprocess.check_output(["cargo", "llvm-cov", "show-env", "--sh"], text=True, env=environment)
    for line in settings.splitlines():
        if line.startswith("export "):
            key, value = line[7:].split("=", 1)
            environment[key] = shlex.split(value)[0]
    # A real Cargo fingerprint keeps coverage artifacts out of ordinary builds.
    environment.pop("RUSTC_WRAPPER", None)
    environment["RUSTFLAGS"] = "-C instrument-coverage"
    environment["OTELC_COVERAGE_BIN_DIR"] = str(Path("target/coverage-build/native-binaries").resolve())
    if environment.get("LLVM_COV"):
        tools = Path(environment["LLVM_COV"]).parent
        environment["CC"] = str(tools / "clang")
        environment["PATH"] = str(tools) + os.pathsep + environment["PATH"]
    return environment


def merge_line_reports(reports):
    """Unify source lines across native/test maps without symbol-name collisions."""
    lines = {}
    for report in reports:
        source = ""
        for line in report.splitlines():
            if line.startswith("SF:"):
                source = str(Path(line[3:]).resolve())
            elif line.startswith("DA:"):
                number, count, *_ = line[3:].split(",")
                key = (source, int(number))
                lines[key] = max(lines.get(key, 0), int(count))
    if not lines:
        raise ValueError("Coverage report contains no instrumented lines")
    records = []
    for source in sorted({key[0] for key in lines}):
        data = sorted((number, count) for (path, number), count in lines.items() if path == source)
        records.extend(["SF:" + source, *(f"DA:{number},{count}" for number, count in data), f"LF:{len(data)}", f"LH:{sum(count > 0 for _, count in data)}", "end_of_record"])
    return "\n".join(records) + "\n"


def line_coverage(lcov):
    """Count instrumented lines; an empty report is a failure, never 100%."""
    canonical = merge_line_reports([lcov])
    counts = [int(line.split(",")[1]) for line in canonical.splitlines() if line.startswith("DA:")]
    return 100 * sum(count > 0 for count in counts) / len(counts)


def write_reports(environment):
    """Include retained native executable maps when merging process profiles."""
    target = Path(environment["CARGO_TARGET_DIR"])
    profiles = list(target.rglob("*.profraw"))
    if not profiles:
        raise ValueError("No execution profiles were generated")
    build = Path("build")
    build.mkdir(exist_ok=True)
    merged = build / "rust-coverage.profdata"
    subprocess.run([environment.get("LLVM_PROFDATA", "llvm-profdata"), "merge", "-sparse", *map(str, profiles), "-o", str(merged)], check=True, env=environment)
    objects = [path for path in (target / "debug/deps").iterdir() if path.is_file() and path.suffix == "" and os.access(path, os.X_OK)]
    objects.append(target / "debug/quux-otelc")
    native = list(Path(environment["OTELC_COVERAGE_BIN_DIR"]).glob("*"))
    native.extend((target / "debug").glob("libotelc_pass.*"))
    command = [environment.get("LLVM_COV", "llvm-cov"), "export", "--format=lcov", "--instr-profile=" + str(merged), "--ignore-filename-regex=/tests/|build.rs|/.cargo/registry/|/rustlib/src/|/opt/homebrew/.*/include/|/Library/Developer/|/\\.tmp[^/]+/"]
    for path in objects:
        command.extend(["--object", str(path)])
    reports = [subprocess.check_output(command, text=True, env=environment)]
    # Extern C functions have identical names in Rust test and static-library
    # builds, but different coverage hashes. Export each native map separately
    # so LLVM selects its matching hash, then merge by source/line.
    for path in native:
        reports.append(subprocess.check_output(command[:5] + ["--object", str(path)], text=True, env=environment))
    report = merge_line_reports(reports)
    (build / "rust-coverage.lcov").write_text(report)
    percentage = line_coverage(report)
    print(f"Rust/native line coverage: {percentage:.2f}% (minimum 80%)", flush=True)
    if percentage < 80:
        raise ValueError("Rust/native line coverage is below 80%")


def main():
    """Build the instrumented archive before native tests, then enforce coverage."""
    environment = coverage_environment()
    for profile in Path(environment["CARGO_TARGET_DIR"]).rglob("*.profraw"):
        profile.unlink()
    for binary in Path(environment["OTELC_COVERAGE_BIN_DIR"]).glob("*"):
        binary.unlink()
    # Remove old workspace maps while retaining cached third-party dependencies.
    subprocess.run(["cargo", "clean", "-p", "quux-otelc-cli", "-p", "quux-otelc-config", "-p", "quux-otelc-symbols", "-p", "quux-otelc-runtime", "-p", "quux-otelc-export"], env=environment, check=True)
    environment["OTELC_PLUGIN_DIR"] = str(Path(environment["CARGO_TARGET_DIR"]) / "debug")
    subprocess.run(["python3", "ci/build_llvm_plugin.py"], env=environment, check=True)
    for command in (
        ["cargo", "build", "--workspace", "--locked"],
        ["cargo", "test", "--workspace", "--locked", "--", "--test-threads=1"],
    ):
        subprocess.run(command, env=environment, check=True)
    write_reports(environment)


if __name__ == "__main__":
    main()
