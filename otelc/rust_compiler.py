"""Build and bind the pinned original-source Rust compiler and private bridge."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from otelc import native_gate
from otelc.file_inputs import checked_file
from otelc.python_sdk_io import decode

ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'rust1981-llvm22-sync-v1'
MANIFEST = 'rust-compiler-tools.json'
FLOOR = 8 * 1024**3
HOSTS = {('darwin', 'arm64'): 'aarch64-apple-darwin',
         ('linux', 'x86_64'): 'x86_64-unknown-linux-gnu',
         ('linux', 'aarch64'): 'aarch64-unknown-linux-gnu'}


def sources() -> list[Path]:
    """Bind compiler, bridge, locked dependencies and the actual build helper."""
    root = ROOT / 'native/rust'
    return [*sorted(root.rglob('*.rs')), *sorted(root.rglob('Cargo.toml')),
            *sorted(root.rglob('Cargo.lock')), Path(__file__).resolve()]


def environment(sysroot: Path | None = None) -> dict[str, str]:
    """Use explicit compiler inputs, without inherited Rust or telemetry hooks."""
    result = {key: value for key, value in os.environ.items()
            if key in ('RUSTUP_HOME', 'CARGO_HOME')
            or not key.startswith(('RUST', 'CARGO_', 'OTEL', 'LLVM_PROFILE'))}
    if sys.platform == 'linux' and sysroot is not None:
        # ELF RUNPATH does not cover the rustc driver's indirect LLVM dependency.
        # Callers obtain this sysroot from their verified compiler-tool record.
        result['LD_LIBRARY_PATH'] = str(sysroot / 'lib')
    return result


def physical_within(path: Path, root: Path) -> bool:
    """Check spelling and existing ancestor identities, including case aliases."""
    path = path.resolve()
    root = root.resolve(strict=True)
    if path.is_relative_to(root):
        return True
    original = root.stat()
    for parent in (path, *path.parents):
        try:
            current = parent.stat()
        except FileNotFoundError:
            continue
        if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
            return True
    return False


def execute(arguments: list[str], output: Path, label: str,
            env: dict[str, str], *, cwd: Path = ROOT) -> bytes:
    """Retain commands and diagnostics; stop only our compiler group on low space."""
    if shutil.disk_usage(output).free < FLOOR:
        raise ValueError('Rust compilation requires at least 8 GiB free')
    command_file = output / (label + '.command.json')
    command_file.write_text(json.dumps({'arguments': arguments, 'cwd': str(cwd)}, indent=2) + '\n')
    started = time.monotonic()
    with (output / (label + '.stdout')).open('xb') as out, (output / (label + '.stderr')).open('xb') as err:
        process = subprocess.Popen(arguments, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err, start_new_session=True)
        try:
            while process.poll() is None:
                if shutil.disk_usage(output).free < FLOOR:
                    raise ValueError('Rust compiler stopped below the 8 GiB storage floor')
                if time.monotonic() - started > 1200:
                    raise ValueError('Rust compiler exceeded its 1200-second build budget')
                try:
                    process.wait(timeout=.25)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            (output / (label + '.result.json')).write_text(json.dumps({
                'pid': process.pid, 'exit_code': process.returncode,
                'seconds': time.monotonic() - started}) + '\n')
    if process.returncode:
        diagnostics = output / (label + '.stderr')
        with diagnostics.open('rb') as stream:
            stream.seek(max(0, diagnostics.stat().st_size - 4096))
            tail = stream.read(4096).decode('utf-8', errors='replace')
        raise ValueError(f'Rust compilation failed (exit {process.returncode}); '
                         f'inspect {diagnostics}; stderr tail: {tail!r}')
    return (output / (label + '.stdout')).read_bytes()


@dataclass(frozen=True)
class Tools:
    """Immutable expected compiler-tool record, separate from application policy."""
    directory: Path
    encoded: str

    def manifest(self) -> dict[str, Any]:
        """Return a fresh metadata object without sharing mutable aliases."""
        return decode(self.encoded.encode())


def build(output: Path) -> Tools:
    """Build fixed offline Cargo recipes with the matching Rust compiler tools."""
    host = HOSTS.get((sys.platform, platform.machine()))
    if host is None:
        raise ValueError('Rust compiler requires macOS ARM64 or 64-bit GNU/Linux')
    output = output.resolve()
    if physical_within(output, ROOT / 'native/rust'):
        raise ValueError('Rust tool output must be outside its source directory')
    before = [native_gate.identity(path) for path in sources()]
    output.mkdir(mode=0o700)
    env = environment()
    rustup = shutil.which('rustup')
    if rustup is None:
        raise ValueError('install Rust 1.98.1 with rustc-dev before building compiler tools')
    rustc = Path(execute([rustup, 'which', '--toolchain', '1.98.1', 'rustc'],
                         output, 'rustc-path', env).decode().strip()).resolve(strict=True)
    version = execute([str(rustc), '-vV'], output, 'rustc-version', env).decode()
    if ('release: 1.98.1\n' not in version or 'LLVM version: 22.1.8\n' not in version
            or 'host: ' + host + '\n' not in version):
        raise ValueError('Rust compiler requires the exact Rust1.98.1/LLVM22.1.8 host profile')
    sysroot = Path(execute([str(rustc), '--print', 'sysroot'], output, 'rustc-sysroot', env)
                   .decode().strip()).resolve(strict=True)
    cargo = sysroot / 'bin/cargo'
    libraries = sorted((sysroot / 'lib').glob('librustc_driver-*'))
    if rustc != sysroot / 'bin/rustc' or len(libraries) != 1 or not libraries[0].is_file():
        raise ValueError('Rust compiler tools do not match their private sysroot')
    tools = [native_gate.identity(path) for path in (rustc, cargo, *libraries)]
    env.update(RUSTC=str(rustc), RUSTC_BOOTSTRAP='otelc_rust_compiler', CARGO_INCREMENTAL='0',
               RUSTFLAGS=f'-C prefer-dynamic -L native={sysroot}/lib '
                         f'-C rpath -C link-arg=-Wl,-rpath,{sysroot}/lib')
    execute([str(cargo), 'build', '--offline', '--locked', '--manifest-path',
             str(ROOT / 'native/rust/compiler/Cargo.toml'), '--target-dir', str(output / 'compiler')],
            output, 'compiler-build', env)
    execute([str(cargo), 'build', '--offline', '--locked', '--release', '--manifest-path',
             str(ROOT / 'native/rust/bridge/Cargo.toml'), '--target-dir', str(output / 'bridge')],
            output, 'bridge-build', env | {'RUSTFLAGS': ''})
    if before != [native_gate.identity(path) for path in sources()]:
        raise ValueError('Rust compiler source changed while building')
    if tools != [native_gate.identity(Path(row['path'])) for row in tools]:
        raise ValueError('Rust compiler tool changed while building')
    value = {'schema_version': 1, 'kind': 'otelc.rust.compiler-tools', 'profile': PROFILE,
             'sysroot': str(sysroot), 'host': host, 'version': version, 'sources': before,
             'tools': tools, 'products': [native_gate.identity(output / name) for name in
                 ('compiler/debug/otelc-rust-compiler', 'bridge/release/libotelc_rust_bridge.rlib')],
             'source_complete': False}
    path = output / MANIFEST
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    return read(output)


def verify(value: Tools) -> dict[str, Any]:
    """Recheck original source, tool and product bindings without compiling."""
    if type(value) is not Tools:
        raise ValueError('Rust compiler tools require a caller-held record')
    _, raw = checked_file(value.directory / MANIFEST, nofollow=True, include_content=True,
                          max_bytes=8 * 1024 * 1024)
    if raw.decode() != value.encoded:
        raise ValueError('Rust compiler tool record changed')
    data = value.manifest()
    fields = {'schema_version', 'kind', 'profile', 'sysroot', 'host', 'version',
              'sources', 'tools', 'products', 'source_complete'}
    if (set(data) != fields or type(data['schema_version']) is not int or data['schema_version'] != 1
            or data['kind'] != 'otelc.rust.compiler-tools' or data['profile'] != PROFILE
            or data['source_complete'] is not False
            or data['host'] != HOSTS.get((sys.platform, platform.machine()))
            or type(data['version']) is not str or type(data['sysroot']) is not str
            or 'release: 1.98.1\n' not in data['version'] or 'LLVM version: 22.1.8\n' not in data['version']
            or 'host: ' + data['host'] + '\n' not in data['version']
            or data['sources'] != [native_gate.identity(path) for path in sources()]):
        raise ValueError('Rust compiler tool profile or source binding changed')
    sysroot = Path(data['sysroot'])
    expected = [sysroot / 'bin/rustc', sysroot / 'bin/cargo',
                *sorted((sysroot / 'lib').glob('librustc_driver-*'))]
    if (not sysroot.is_absolute() or sysroot.resolve(strict=True) != sysroot or len(expected) != 3
            or data['tools'] != [native_gate.identity(path) for path in expected]
            or data['products'] != [native_gate.identity(value.directory / name) for name in
                 ('compiler/debug/otelc-rust-compiler', 'bridge/release/libotelc_rust_bridge.rlib')]):
        raise ValueError('Rust compiler tool inputs or products changed')
    return data


def read(directory: Path) -> Tools:
    """Read the fixed tool record and reject stale/substituted inputs before use."""
    directory = directory.resolve(strict=True)
    _, raw = checked_file(directory / MANIFEST, nofollow=True, include_content=True,
                          max_bytes=8 * 1024 * 1024)
    result = Tools(directory, raw.decode())
    verify(result)
    return result


def main() -> None:
    """Build compiler tools without loading an application instrumentation policy."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(build(arguments.output).manifest(), indent=2))


if __name__ == '__main__':
    main()
