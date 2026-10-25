"""Build and check the pinned internal Rust compiler against its genuine SDK."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

from ci.check_native_controls import summaries
from ci.check_native_launch import test_result
from ci import rust_sonar
from otelc import native_build, native_launch

ROOT = Path(__file__).resolve().parents[1]
FLOOR = 8 * 1024**3


def run(arguments: list[str], work: Path, label: str, environment: dict[str, str]) -> bytes:
    """Keep commands and failures, and refuse new builds below the storage floor."""
    if shutil.disk_usage(work).free < FLOOR:
        raise ValueError('Rust compiler checks require at least 8 GiB free')
    result = subprocess.run(arguments, cwd=ROOT, env=environment, capture_output=True, timeout=1200)
    (work / (label + '.stdout')).write_bytes(result.stdout)
    (work / (label + '.stderr')).write_bytes(result.stderr)
    (work / (label + '.json')).write_text(json.dumps({'arguments': arguments,
        'exit_code': result.returncode}, indent=2) + '\n')
    result.check_returncode()
    return result.stdout


def check() -> None:
    """Keep this component distinct from public Rust adapter/lifetime admission."""
    host = (sys.platform, platform.machine())
    if host not in (('darwin', 'arm64'), ('linux', 'x86_64'), ('linux', 'aarch64')):
        raise ValueError('Rust compiler checks require macOS ARM64 or 64-bit GNU/Linux')
    (ROOT / 'build').mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='rust-compiler-check-', dir=ROOT / 'build')).resolve()
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('OTEL', 'LLVM_PROFILE'))
                   and key not in ('RUSTFLAGS', 'RUSTC', 'RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER')}
    rustc = run(['rustup', 'which', '--toolchain', '1.98.1', 'rustc'], work, 'rustc-path', environment).decode().strip()
    version = run([rustc, '-vV'], work, 'rustc-version', environment).decode()
    if 'release: 1.98.1\n' not in version or 'LLVM version: 22.1.8\n' not in version:
        raise ValueError('Rust compiler checks require Rust1.98.1/LLVM22.1.8')
    sysroot = run([rustc, '--print', 'sysroot'], work, 'rustc-sysroot', environment).decode().strip()
    sdk_directory = os.environ.get('OTELC_NATIVE_SDK_BUILD')
    sdk = Path(sdk_directory).resolve(strict=True) if sdk_directory else work / 'sdk'
    if not sdk_directory:
        native_launch.build_sdk(sdk, coverage=True)
    record = native_build.sdk(sdk)
    sources = rust_sonar.source_paths(ROOT)
    before = [native_launch.identity(path) for path in sources]
    binaries = Path(record['toolchain']['directory'])
    tools = [Path(rustc), Path(sysroot) / 'bin/cargo-clippy',
             binaries / 'llvm-profdata', binaries / 'llvm-cov']
    held_tools = [native_launch.identity(path) for path in tools]
    environment.update(RUSTC=rustc, RUSTC_BOOTSTRAP='otelc_rust_compiler',
        RUSTFLAGS=f'-C prefer-dynamic -L native={sysroot}/lib -C rpath '
                  f'-C link-arg=-Wl,-rpath,{sysroot}/lib -C instrument-coverage',
        LLVM_PROFILE_FILE=str(work / 'unit-%m-%p.profraw'))
    cargo = ['rustup', 'run', '1.98.1', 'cargo']
    compiler_flags = ['--offline', '--locked', '--manifest-path', 'native/rust/compiler/Cargo.toml',
                      '--target-dir', str(work / 'compiler')]
    run([*cargo, 'build', *compiler_flags], work, 'compiler-build', environment)
    unit = run([*cargo, 'test', *compiler_flags], work, 'compiler-unit', environment)
    unit_result = re.search(rb'test result: ok\. (\d+) passed; 0 failed; 0 ignored;', unit)
    if unit_result is None or int(unit_result[1]) < 9:
        raise ValueError('Rust compiler request tests were missing or skipped')
    clippy = str(Path(sysroot) / 'bin/cargo-clippy')
    compiler_clippy = run([clippy, 'clippy', *compiler_flags, '--all-targets', '--message-format=json',
         '--', '-D', 'warnings'], work, 'compiler-clippy', environment)
    run([*cargo, 'build', '--offline', '--locked', '--release', '--manifest-path',
         'native/rust/bridge/Cargo.toml', '--target-dir', str(work / 'bridge')], work, 'bridge-build',
        environment | {'RUSTFLAGS': '-C instrument-coverage'})
    key_environment = environment | {'RUSTFLAGS': f'-C instrument-coverage -L native={sdk} '
        f'-l dylib=otelc_native_runtime -C link-arg=-Wl,-rpath,{sdk}'}
    key_build = run([*cargo, 'test', '--offline', '--locked', '--no-run', '--message-format=json',
        '--manifest-path', 'native/rust/bridge/Cargo.toml', '--target-dir', str(work / 'bridge')],
        work, 'bridge-unit-build', key_environment)
    key_units = [Path(row['executable']).resolve(strict=True)
                 for row in map(json.loads, key_build.splitlines())
                 if row.get('reason') == 'compiler-artifact'
                 and row.get('target', {}).get('name') == 'otelc_rust_bridge'
                 and row.get('profile', {}).get('test') is True and row.get('executable')]
    if len(key_units) != 1 or not key_units[0].is_relative_to(work / 'bridge'):
        raise ValueError('Rust bridge key tests require one test executable from this build')
    key_unit = run([str(key_units[0])], work, 'bridge-unit', key_environment)
    if re.search(rb'test result: ok\. 3 passed; 0 failed; 0 ignored;', key_unit) is None:
        raise ValueError('Rust observation-key tests were missing or skipped')
    bridge_clippy = run([clippy, 'clippy', '--offline', '--locked', '--all-targets', '--message-format=json',
        '--manifest-path', 'native/rust/bridge/Cargo.toml', '--target-dir', str(work / 'bridge'),
        '--', '-D', 'warnings'], work, 'bridge-clippy', environment | {'RUSTFLAGS': '-C instrument-coverage'})
    cases = work / 'cases'
    environment.update(PYTHONPATH=str(ROOT) + os.pathsep + str(ROOT / 'tests'),
        OTELC_RUST_COMPONENT_OUTPUT=str(cases), OTELC_RUSTC=rustc,
        OTELC_RUST_COMPILER=str(work / 'compiler/debug/otelc-rust-compiler'),
        OTELC_RUST_BRIDGE=str(work / 'bridge/release/libotelc_rust_bridge.rlib'), OTELC_NATIVE_SDK_BUILD=str(sdk))
    data = work / 'python.coverage'
    run([sys.executable, '-m', 'coverage', 'run', '--data-file=' + str(data),
         '--source=ci.rust_compiler_cases,ci.rust_lifetime_cases,ci.rust_drop_cases,otelc.rust_lifetimes,otelc.health',
         '-m', 'ci.rust_compiler_cases'], work, 'cases', environment)
    tests = json.loads((cases / 'tests-result.json').read_bytes())
    test_result(tests)
    for name in ('ci/rust_compiler_cases.py', 'ci/rust_lifetime_cases.py', 'ci/rust_drop_cases.py',
                 'otelc/rust_lifetimes.py', 'otelc/health.py'):
        run([sys.executable, '-m', 'coverage', 'report', '--data-file=' + str(data),
             '--include=*/' + name, '--fail-under=80'], work, Path(name).stem + '-coverage', environment)
    profiles = sorted([*cases.glob('*.profraw'), *work.glob('unit-*.profraw')])
    if not profiles:
        raise ValueError('Rust compiler execution produced no coverage profiles')
    merged = work / 'compiler.profdata'
    run([str(binaries / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles), '-o', str(merged)],
        work, 'profiles', environment)
    expected = {ROOT / 'native/rust/compiler/src' / name
                for name in ('main.rs', 'request.rs', 'hooks.rs', 'instrument.rs', 'lifetimes.rs', 'drops.rs')}
    raw = run([str(binaries / 'llvm-cov'), 'export', environment['OTELC_RUST_COMPILER'],
        '-instr-profile=' + str(merged), *map(str, sorted(expected))], work, 'compiler-coverage', environment)
    coverage = summaries(json.loads(raw), expected)
    applications = list((cases / 'test_original_modules_optimised_loops_panics_and_all_live_metric_states').glob('*-application'))
    drop_case = cases / 'test_private_drop_bridge_moves_panics_forgets_and_refuses_bad_metadata'
    drop_applications = list(drop_case.glob('*-application'))
    keys_case = cases / 'test_private_drop_keys_distinguish_zero_size_moves_and_forgotten_values'
    keys_applications = list(keys_case.glob('*-application'))
    automatic_case = cases / 'test_automatic_drop_mir_preserves_stock_moves_zero_size_and_panic_paths'
    automatic_applications = list(automatic_case.glob('*-application'))
    health = list(cases.rglob('health.json'))
    if (len(applications) != 2 or len(drop_applications) != 1 or len(keys_applications) != 1
            or len(automatic_applications) != 2 or len(health) != 24
            or len(list(drop_case.rglob('health.json'))) != 4
            or len(list(keys_case.rglob('health.json'))) != 4
            or len(list(automatic_case.rglob('health.json'))) != 8):
        raise ValueError('Rust checks require original8, private drop4/key4 and automatic lifetime8 launches')
    bridge_source = ROOT / 'native/rust/bridge/src/lib.rs'
    bridge_objects = [str(sorted(applications)[0]), '-object=' + str(drop_applications[0]),
                      '-object=' + str(keys_applications[0]), '-object=' + str(key_units[0]),
                      *('-object=' + str(path) for path in sorted(automatic_applications))]
    bridge_raw = run([str(binaries / 'llvm-cov'), 'export', *bridge_objects,
        '-instr-profile=' + str(merged), str(bridge_source)], work, 'bridge-coverage', environment)
    bridge_coverage = summaries(json.loads(bridge_raw), {bridge_source})
    compiler_lcov = run([str(binaries / 'llvm-cov'), 'export', environment['OTELC_RUST_COMPILER'],
        '-instr-profile=' + str(merged), '-format=lcov', *map(str, sorted(expected))],
        work, 'compiler-lcov', environment).decode()
    bridge_lcov = run([str(binaries / 'llvm-cov'), 'export', *bridge_objects,
        '-instr-profile=' + str(merged), '-format=lcov', str(bridge_source)], work, 'bridge-lcov', environment).decode()
    if (before != [native_launch.identity(path) for path in sources] or native_build.sdk(sdk) != record
            or held_tools != [native_launch.identity(path) for path in tools]):
        raise ValueError('Rust compiler/bridge or genuine SDK inputs changed during checks')
    result = {'tests': tests, 'source_inputs': before, 'tool_inputs': held_tools, 'sdk_library': record['library'],
              'compiler_line_coverage': coverage, 'bridge_line_coverage': bridge_coverage,
              'application_processes': len(health),
              'original_application_processes': 8, 'private_drop_harness_processes': 4,
              'private_key_harness_processes': 4, 'observation_key_unit_tests': 3,
              'automatic_lifetime_application_processes': 8,
              'telemetry_evidence': 'Receiver-decoded protobuf reserialization, not original HTTP wire bytes',
              'scope': 'Internal synchronous compiler and explicit drop/key bridge/SDK harnesses; not automatic Rust lifetime admission'}
    (work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    rust_sonar.write(ROOT, before, compiler_lcov + bridge_lcov, coverage | bridge_coverage,
                     compiler_clippy + bridge_clippy, ROOT / 'build')
    rust_sonar.verify(ROOT, ROOT / 'build')
    shutil.copyfile(data, ROOT / 'build/rust-case.coverage')
    print(json.dumps({'work': str(work), 'coverage': coverage}))


if __name__ == '__main__':
    check()
