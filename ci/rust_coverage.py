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
    fields = {key: value for key, separator, value in (line.partition(": ") for line in version.splitlines()) if separator}
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


def write_reports(environment, extra_reports=()):
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
    objects.extend([target / "debug/quux-otelc", target / "debug/otelc-rust-adapter"])
    native = list(Path(environment["OTELC_COVERAGE_BIN_DIR"]).glob("*"))
    native.extend((target / "debug").glob("libotelc_pass.*"))
    native.extend((target / "debug").glob("otelc_node_observer.node"))
    command = [environment.get("LLVM_COV", "llvm-cov"), "export", "--format=lcov", "--instr-profile=" + str(merged), "--ignore-filename-regex=/tests/|build.rs|/.cargo/registry/|/rustlib/src/|/opt/homebrew/.*/include/|/include/node/|/Library/Developer/|/\\.tmp[^/]+/"]
    # Extern C functions have identical names in Rust test and static-library
    # builds, but different coverage hashes. This also affects the compiler ABI
    # in Rust-only executables. Isolate every map so LLVM selects its matching
    # profile, then merge the observed hits by source/line.
    reports = [subprocess.check_output(command + ["--object", str(path)], text=True, env=environment)
               for path in [*objects, *native]]
    report = merge_line_reports([*reports, *extra_reports])
    (build / "rust-coverage.lcov").write_text(report)
    percentage = line_coverage(report)
    print(f"Rust/native line coverage: {percentage:.2f}% (minimum 80%)", flush=True)
    if percentage < 80:
        raise ValueError("Rust/native line coverage is below 80%")
    enforce_rust_adapter_coverage(report)
    enforce_node_observer_coverage(report)
    enforce_rust_file_coverage(report)


def enforce_rust_adapter_coverage(report):
    """Prevent older native code from masking an untested Rust language adapter."""
    for crate in ("rust-adapter", "rust-probes", "process"):
        selected = []
        include = False
        for line in report.splitlines():
            if line.startswith("SF:"):
                include = f"/crates/{crate}/src/" in line
            if include:
                selected.append(line)
        percentage = line_coverage("\n".join(selected))
        print(f"Rust {crate} line coverage: {percentage:.2f}% (minimum 80%)", flush=True)
        if percentage < 80:
            raise ValueError(f"Rust {crate} line coverage is below 80%")


def enforce_node_observer_coverage(report):
    """Give the native Node observer its own product coverage gate."""
    selected = []
    include = False
    for line in report.splitlines():
        if line.startswith("SF:"):
            include = "/native/node/" in line
        if include:
            selected.append(line)
    percentage = line_coverage("\n".join(selected))
    print(f"Node native observer line coverage: {percentage:.2f}% (minimum 80%)", flush=True)
    if percentage < 80:
        raise ValueError("Node native observer line coverage is below 80%")


def enforce_rust_file_coverage(report):
    """Require independent floors for trace, artefact and compiler-hook boundaries."""
    for path, label in (("/crates/rust-probes/src/traces.rs", "Rust trace context"),
                        ("/crates/export/src/traces.rs", "Shared trace store"),
                        ("/crates/runtime/src/traces.rs", "Native trace bridge"),
                        ("/crates/rust-adapter/src/artifact.rs", "Rust build artifacts"),
                        ("/crates/rust-probes/src/compiler_abi.rs", "Rust compiler ABI"),
                        ("/crates/rust-probes/src/compiler_mir.rs", "Rust compiler future observers"),
                        ("/crates/rust-probes/src/recording.rs", "Rust SDK recording suppression"),
                        ("/crates/rust-adapter/src/compiler_mir.rs", "Rust original-source compiler adapter"),
                        ("/crates/rust-adapter/src/compiler_mir_profile.rs", "Rust stable compiler profile"),
                        ("/crates/rust-adapter/src/declarations.rs", "Rust original declaration parser"),
                        ("/crates/rust-adapter/src/compiler_inventory.rs", "Rust compiler inventory"),
                        ("/crates/export/src/carrier.rs", "Shared trace carrier"),
                        ("/crates/symbols/src/object_transform.rs", "Native object transforms"),
                        ("/crates/cli/src/object_command.rs", "Native object operation")):
        selected = []
        include = False
        for line in report.splitlines():
            if line.startswith("SF:"):
                include = line.endswith(path)
            if include:
                selected.append(line)
        percentage = line_coverage("\n".join(selected))
        print(f"{label} line coverage: {percentage:.2f}% (minimum 80%)", flush=True)
        if percentage < 80:
            raise ValueError(f"{label} line coverage is below 80%")


def main():
    """Build the instrumented archive before native tests, then enforce coverage."""
    from ci.rust_llvm_coverage import run as compiler_coverage
    from ci.rust_mir_coverage import run as mir_coverage, source_hashes
    from ci import build_rust_mir
    environment = coverage_environment()
    before = source_hashes(Path.cwd())
    for profile in Path(environment["CARGO_TARGET_DIR"]).rglob("*.profraw"):
        profile.unlink()
    for binary in Path(environment["OTELC_COVERAGE_BIN_DIR"]).glob("*"):
        binary.unlink()
    # Remove old workspace maps while retaining cached third-party dependencies.
    subprocess.run(["cargo", "clean", "-p", "quux-otelc-cli", "-p", "quux-otelc-config", "-p", "quux-otelc-symbols", "-p", "quux-otelc-runtime", "-p", "quux-otelc-export", "-p", "quux-otelc-rust", "-p", "quux-otelc-rust-adapter"], env=environment, check=True)
    environment["OTELC_PLUGIN_DIR"] = str(Path(environment["CARGO_TARGET_DIR"]) / "debug")
    subprocess.run(["python3", "ci/build_llvm_plugin.py"], env=environment, check=True)
    subprocess.run(["python3", "ci/build_node_observer.py"], env=environment, check=True)
    environment["OTELC_NODE_OBSERVER"] = str(Path(environment["CARGO_TARGET_DIR"]) / "debug/otelc_node_observer.node")
    subprocess.run(["cargo", "build", "--workspace", "--locked"], env=environment, check=True)
    # Bind the public adapter to this run's instrumented SDK. The compiler
    # helper itself is built normally; its separate gate instruments its map.
    helper_environment = {key: value for key, value in environment.items()
                          if key not in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS",
                                         "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER")}
    build_rust_mir.build(Path.cwd(), Path(environment["CARGO_TARGET_DIR"]) / "debug",
                        helper_environment)
    for command in (
        ["cargo", "test", "--workspace", "--locked", "--", "--test-threads=1"],
        ["node", "--test", "adapters/node/tests/promise.test.mjs"],
    ):
        subprocess.run(command, env=environment, check=True)
    compiler_report = compiler_coverage()
    mir_report = mir_coverage(stable=environment)
    if source_hashes(Path.cwd()) != before:
        raise ValueError("Rust/native sources changed during coverage qualification")
    write_reports(environment, [compiler_report, mir_report])


if __name__ == "__main__":
    main()
