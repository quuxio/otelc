"""Build a pinned Rust SDK/pass pair without changing application sources."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess


RUST_COMMIT = "d080e7dff1b0fc54541545252818f8cccf995d05"


def output(arguments: list[str]) -> str:
    """Read tool metadata without shell expansion."""
    return subprocess.check_output(arguments, text=True).strip()


def qualified_rustc(program: str) -> Path:
    """Resolve the real compiler after checking its complete qualified identity."""
    located = shutil.which(program)
    if not located:
        raise ValueError("Set OTELC_RUSTC to the qualified nightly rustc")
    version = output([located, "-vV"])
    fields = dict(line.split(": ", 1) for line in version.splitlines() if ": " in line)
    if (not version.startswith("rustc 1.101.0-nightly ")
            or fields.get("commit-hash") != RUST_COMMIT
            or fields.get("host") != "aarch64-apple-darwin"
            or fields.get("LLVM version") != "23.1.1"):
        raise ValueError("Rust LLVM requires 1.101.0-nightly d080e7dff on macOS ARM64 with LLVM 23.1.1")
    # A rustup proxy would change identity with the user's default toolchain.
    compiler = Path(output([located, "--print", "sysroot"])) / "bin/rustc"
    if not compiler.is_file() or output([str(compiler), "-vV"]) != version:
        raise ValueError("Qualified Rust compiler identity changed during setup")
    return compiler.resolve()


def fingerprint(path: Path) -> str:
    """Hash the built input, rather than inferring identity from its filename."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def llvm_tools(config: str) -> Path:
    """Locate the exact LLVM release used to build and read pass profiles."""
    if output([config, "--version"]) != "23.1.3":
        raise ValueError("Rust LLVM pass requires LLVM 23.1.3")
    return Path(output([config, "--bindir"]))


def compile_pass(root: Path, destination: Path, config: str,
                 environment: dict[str, str], coverage: bool = False) -> Path:
    """Compile the same pass for ordinary or isolated coverage execution."""
    bindir = llvm_tools(config)
    plugin = destination / "otelc_rust_pass.dylib"
    command = [str(bindir / "clang++"), "-shared", "-fPIC", "-O2",
               *shlex.split(output([config, "--cxxflags"])),
               str(root / "native/rust/OtelcRustPass.cpp"),
               *shlex.split(output([config, "--ldflags", "--libs", "core", "passes", "--system-libs"])),
               "-o", str(plugin)]
    if coverage:
        command.extend(["-fprofile-instr-generate", "-fcoverage-mapping"])
    subprocess.run(command, check=True, env=environment)
    return plugin


def write_profile(root: Path, destination: Path, rustc: Path,
                  sdk: Path, plugin: Path) -> dict[str, object]:
    """Publish a matching descriptor only after both built inputs exist."""
    profile = {"schema_version": 1, "rustc": str(rustc), "sdk": str(sdk),
               "sdk_sha256": fingerprint(sdk), "plugin": str(plugin),
               "plugin_sha256": fingerprint(plugin), "llvm_version": "23.1.3",
               "protected_source_roots": [str(root / "crates" / name)
                                          for name in ("config", "export", "rust-probes")]}
    (destination / "otelc-rust-llvm.json").write_text(json.dumps(profile, indent=2) + "\n")
    return profile


def build(root: Path, destination: Path, environment: dict[str, str]) -> dict[str, object]:
    """Produce the private SDK, LLVM pass and matching compiler descriptor."""
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_TARGET"):
        if environment.get(key):
            raise ValueError(f"Rust LLVM build requires {key} to be unset")
    rustc = qualified_rustc(environment.get("OTELC_RUSTC", "rustc"))
    config = environment.get("OTELC_RUST_LLVM_CONFIG", "/opt/homebrew/opt/llvm@23/bin/llvm-config")
    llvm_tools(config)
    destination.mkdir(parents=True, exist_ok=True)
    sdk_target = root / "target/rust-llvm-sdk"
    subprocess.run(["cargo", "build", "-p", "quux-otelc-rust", "--lib", "--locked",
                    "--target-dir", str(sdk_target)], cwd=root,
                   env={**environment, "RUSTC": str(rustc)}, check=True)
    plugin = compile_pass(root, destination, config, environment)
    sdk = sdk_target / "debug/libquux_otelc_rust.rlib"
    # Publish metadata last; a failed build cannot silently pair new and old inputs.
    return write_profile(root, destination, rustc, sdk, plugin)


def main() -> None:
    """Build this checkout's explicit experimental compiler profile."""
    root = Path(__file__).resolve().parents[1]
    profile = build(root, (root / "target/debug").resolve(), dict(os.environ))
    print(f"Rust LLVM compiler profile: {profile['rustc']}; {profile['plugin']}")


if __name__ == "__main__":
    main()
