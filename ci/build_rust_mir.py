"""Build compiler-owned probes with an explicitly qualified stable compiler."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


RUST_COMMIT = "48a229ceaefd4985c50990b14116b6d856af0985"
PROFILE = "otelc-rust-compiler.json"


def output(arguments: list[str], environment: dict[str, str]) -> str:
    """Read compiler identity without shell expansion or application bootstrap."""
    return subprocess.check_output(
        arguments, text=True, env=environment).strip()


def qualified_rustc(program: str, environment: dict[str, str]) -> Path:
    """Resolve a proxy only after checking every qualified compiler field."""
    located = shutil.which(program, path=environment.get("PATH"))
    if not located:
        raise ValueError("Install Rust 1.98.1 and its rustc-dev component")
    version = output([located, "-vV"], environment)
    fields = {}
    for line in version.splitlines():
        key, separator, value = line.partition(": ")
        if separator:
            fields[key] = value
    if (not version.startswith("rustc 1.98.1 ")
            or fields.get("commit-hash") != RUST_COMMIT
            or fields.get("host") != "aarch64-apple-darwin"
            or fields.get("LLVM version") != "22.1.8"):
        raise ValueError("Rust compiler probes require qualified Rust 1.98.1 "
                         "48a229cea on macOS ARM64 with LLVM 22.1.8")
    compiler = Path(output([located, "--print", "sysroot"], environment)) / "bin/rustc"
    if not compiler.is_file() or output([str(compiler), "-vV"], environment) != version:
        raise ValueError("Rust compiler identity changed during setup")
    return compiler.resolve()


def fingerprint(path: Path) -> str:
    """Hash the actual build input, including the selected compiler binary."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(root: Path, destination: Path,
          environment: dict[str, str]) -> dict[str, object]:
    """Publish a descriptor last; a failed build cannot advertise new inputs."""
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS",
                "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_TARGET", "RUSTC_BOOTSTRAP"):
        if environment.get(key):
            raise ValueError(f"Rust compiler build requires {key} to be unset")
    compiler = qualified_rustc(environment.get("RUSTC", "rustc"), environment)
    # OTELC_RUSTC selects the optional LLVM backend at launch. It must not
    # change the stable helper when both profiles are built in the same job.
    values = dict(environment, RUSTC=str(compiler))
    library = compiler.parent.parent / "lib"
    destination.mkdir(parents=True, exist_ok=True)
    sdk = destination / "libquux_otelc_rust.rlib"
    if not sdk.is_file():
        raise ValueError("Build the stable Rust SDK before its compiler profile")
    target = root / "target/rust-mir-frontend"
    subprocess.run(
        ["cargo", "rustc", "--manifest-path", str(root / "native/rust/mir/Cargo.toml"),
         "--locked", "--target-dir", str(target), "--bin", "otelc-rust-mir", "--",
         "-C", "rpath", "-C", "link-arg=-Wl,-rpath," + str(library)],
        cwd=root, env=dict(values, RUSTC_BOOTSTRAP="otelc_rust_mir"), check=True)
    driver = destination / "otelc-rust-mir"
    shutil.copy2(target / "debug/otelc-rust-mir", driver)
    profile = {
        "schema_version": 1, "backend": "compiler", "rustc": str(compiler),
        "rustc_sha256": fingerprint(compiler), "commit_hash": RUST_COMMIT,
        "sdk": str(sdk), "sdk_sha256": fingerprint(sdk),
        "driver": str(driver), "driver_sha256": fingerprint(driver),
        "protected_source_roots": [str(root / "crates" / name)
                                   for name in ("config", "export", "rust-probes")],
    }
    temporary = destination / (PROFILE + ".pending")
    temporary.write_text(json.dumps(profile, indent=2) + "\n")
    temporary.replace(destination / PROFILE)
    return profile


def main() -> None:
    """Build a profile alongside this checkout's ordinary stable products."""
    root = Path(__file__).resolve().parents[1]
    profile = build(root, (root / "target/debug").resolve(), dict(os.environ))
    print(f"Rust compiler profile: {profile['rustc']}; {profile['driver']}")


if __name__ == "__main__":
    main()
