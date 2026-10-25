"""Compare the retained ordinary Rust example against four live metric states."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
import platform
import subprocess
import time
from typing import Any, Iterator, TextIO

from otelc import benchmark, benchmark_cleanup, benchmark_process, control, health
from otelc import native_benchmark, native_gate, native_launch, rust_build, rust_compiler, rust_launch
from otelc import python_launch_files as files
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = Path('examples/rust/BenchmarkApp.rs')


@dataclass
class Application:
    """The direct Rust child includes its embedded SDK; there is no supervisor."""

    owner: subprocess.Popen[str]
    created_at_monotonic_ns: int
    first_response: dict[str, Any] | None = None
    sdk_report: dict[str, Any] | None = None
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def pid(self) -> int:
        return self.owner.pid

    @property
    def stdin(self) -> TextIO | None:
        return self.owner.stdin

    @property
    def stdout(self) -> TextIO | None:
        return self.owner.stdout

    def poll(self) -> int | None:
        return self.owner.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.owner.wait(timeout=timeout)

    def kill(self) -> None:
        self.owner.kill()


def final_health(process: Application, runtime: Path, built: rust_build.Build, policy: Policy) -> None:
    """Require build-bound final health and a clean native SDK, not just its shape."""
    report = health.read(runtime / 'health.json')
    rust_launch.validate_health(report, built, policy, process.pid)
    native_benchmark.validate_drain(report, policy)
    if int(report['function_accounting']['started']) == 0:
        raise ValueError('Rust benchmark observed no selected function calls')
    process.sdk_report = report


@contextmanager
def application(policy: Policy, built: rust_build.Build, runtime: Path, *, plain: bool) -> Iterator[Application]:
    """Reap and validate even after a failed sample, preserving the first failure."""
    with runtime.with_suffix('.stderr').open('w') as errors:
        created = time.monotonic_ns()
        owner = rust_launch.start(built, policy, runtime, plain=plain, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, start_new_session=True)
        process = Application(owner, created)
        failure = None
        try:
            files.write(runtime / 'process.json', {'schema_version': 1, 'pid': process.pid,
                'sdk_pid': None if plain else process.pid, 'plain': plain,
                'scope': 'direct child; not prepared host-release or whole-program provenance'})
            yield process
        except BaseException as error:
            failure = error
            raise
        finally:
            cleanup = benchmark_cleanup.Cleanup(failure)
            cleanup.run(lambda: benchmark.stop(process), 'Rust benchmark application stop')
            cleanup.run(lambda: rust_build.verify(built, policy), 'Rust benchmark input verification')
            if not plain:
                cleanup.run(lambda: final_health(process, runtime, built, policy), 'Rust benchmark final SDK validation')
            cleanup.finish()
        names = ('process.json', 'rust-first-response.json') + (() if plain else ('plan.json', 'health.json'))
        process.evidence = {name: native_gate.identity(runtime / name) for name in names}


def measure(policy: Policy, built: rust_build.Build, output: Path, *, plain: bool,
            records: benchmark.Measurements) -> Application:
    """Keep startup and flush outside the application's integer-workload timer."""
    runtime = output / ('plain' if plain else 'instrumented')
    endpoint = None if plain else runtime / 'control.json'
    expected = benchmark.checksum(policy.data['benchmark']['iterations'])
    with application(policy, built, runtime, plain=plain) as process:
        if endpoint is not None:
            control.wait_ready(endpoint, process.pid)
            benchmark.switch_state(endpoint, process, 'both_off')
        response = benchmark.batch(process, 1)
        process.first_response = {'elapsed_ns': time.monotonic_ns() - process.created_at_monotonic_ns,
            'calls': 1, 'response': response,
            'scope': 'direct process creation through first workload response; includes SDK readiness, controls, IPC and body'}
        files.write(runtime / 'rust-first-response.json', process.first_response)
        if response['checksum'] != benchmark.checksum(1):
            raise ValueError('Rust benchmark startup checksum differs')
        for _ in range(policy.data['benchmark']['warmup']):
            if benchmark.batch(process, policy.data['benchmark']['iterations'])['checksum'] != expected:
                raise ValueError('Rust benchmark warmup checksum differs')
        if endpoint is not None:
            control.request(endpoint, 'flush')
        benchmark_process.measure_batches(process, policy, output, endpoint, None, expected, records)
        if endpoint is not None:
            benchmark_process.finalize_metrics(process, endpoint, None, output)
    return process


def run(policy: Policy, output: Path, *, build_directory: Path,
        comparison_id: str | None = None, publish: bool = True) -> dict[str, Any]:
    """Use fixed stock/woven products of the unchanged common-workload example."""
    built = rust_build.read(build_directory, policy)
    metadata = built.manifest()
    root = Path(metadata['root'])
    original = native_gate.identity(ROOT / EXAMPLE)
    if (Path(metadata['entry']) != root / EXAMPLE or not metadata['selected']
            or len(metadata['inputs']) != 1 or metadata['inputs'][0]['path'] != str(root / EXAMPLE)
            or metadata['inputs'][0]['sha256'] != original['sha256']):
        raise ValueError('Rust comparison requires the unchanged shared examples/rust/BenchmarkApp.rs')
    output = output.resolve()
    if (rust_compiler.physical_within(output, root)
            or rust_compiler.physical_within(output, built.directory)):
        raise ValueError('Rust benchmark output must be outside original sources and retained build')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    records = benchmark.Measurements()
    processes = {lane: measure(policy, built, output, plain=lane == 'plain', records=records)
                 for lane in ('plain', 'instrumented')}
    for lane, process in processes.items():
        for name, expected in process.evidence.items():
            if native_gate.identity(output / lane / name) != expected:
                raise ValueError('Rust retained benchmark evidence changed')
    rust_build.verify(built, policy)
    if native_gate.identity(ROOT / EXAMPLE) != original:
        raise ValueError('Rust maintained benchmark example changed')
    tools = rust_compiler.read(Path(metadata['tools']['path']).parent).manifest()
    identity = benchmark_process.RunIdentity('rust', 'native ' + platform.platform(), tools['version'].strip(),
        ('--edition=2024', '-Copt-level=' + str(metadata['optimisation']), '-g'), original['sha256'])
    report = benchmark_process.summary(policy, output, records, identity,
        {lane: process.pid for lane, process in processes.items()}, None, comparison_id)
    report.update(launch_profile=rust_build.PROFILE, source_complete=False,
        sdk_process_model='embedded; application CPU/RSS includes SDK; counted once',
        config_sha256=native_launch.digest(policy.data), build=metadata,
        build_identity=native_gate.identity(built.directory / rust_build.MANIFEST),
        launch_to_first_response={lane: process.first_response for lane, process in processes.items()},
        launch_evidence={lane: process.evidence for lane, process in processes.items()},
        sdk_health=processes['instrumented'].sdk_report, downstream_receipt='unavailable',
        baseline={'plain': 'direct original stock executable; no SDK, probes, bootstrap or supervisor',
                  'instrumented': 'direct woven executable; embedded C++ SDK, no separate supervisor'},
        metric_finalization=native_gate.identity(output / 'metric-finalization.json'))
    files.write(output / 'report.json', report)
    if publish:
        benchmark.export_result(report, policy.data['export']['endpoint'])
    return report
