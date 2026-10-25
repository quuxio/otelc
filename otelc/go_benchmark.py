"""Compare prebuilt stock/woven Go applications through the public owned recipe."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, TextIO

from otelc import benchmark, benchmark_process, control, go_health, path_boundaries, prepared_go as prepared
from otelc import python_launch_files as files
from otelc.config import Policy
from otelc.file_inputs import identity
from otelc.prepared_python import stop_group


@dataclass
class Application:
    """Observe the native application separately from its fixed supervisor."""

    owner: subprocess.Popen[str]
    pid: int
    created_at_monotonic_ns: int
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    first_response: dict[str, Any] | None = None

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


def started(owner: subprocess.Popen[str], runtime: Path, created: int, *, plain: bool,
            timeout: float = 20.) -> Application:
    """Check the actual owner and live group, rather than trusting a response PID."""
    request, plan, _ = prepared.verify(runtime)
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = prepared.read(runtime / 'go-application-start.json')
            break
        except FileNotFoundError:
            if owner.poll() is not None:
                raise RuntimeError('Go benchmark exited before application readiness')
            if time.monotonic() >= deadline:
                raise TimeoutError('Go benchmark application ownership is unavailable')
            time.sleep(.01)
    pid = value.get('pid')
    expected = {'schema_version': 1, 'kind': 'otelc.go.application', 'profile': prepared.PROFILE,
                'launch_id': request['launch_id'], 'pid': pid, 'supervisor_pid': owner.pid,
                'process_group': owner.pid, 'plain': plain, 'argv': plan['argv'], 'binary': plan['binary']}
    if (type(pid) is not int or pid <= 1 or pid == owner.pid
            or not prepared.same(value, expected) or owner.poll() is not None
            or os.getpgid(pid) != owner.pid):
        raise ValueError('Go benchmark application differs from its actual owned group')
    return Application(owner, pid, created)


def completed(process: Application, runtime: Path, *, plain: bool, retain: bool = True) -> None:
    """Require native exit and checked SDK drain; downstream receipt stays separate."""
    result = prepared.execution(runtime, supervisor_pid=process.owner.pid,
                                parent_pid=os.getpid(), returncode=process.owner.returncode)
    if (result['application_pid'] != process.pid or result['application_exit_code'] != 0
            or result['interrupted_by'] is not None or not result['inputs_rechecked'] or result['failures']):
        raise ValueError('Go benchmark final execution is incomplete')
    if not plain and not go_health.final_clean(result['sdk_health']):
        raise ValueError('Go benchmark final SDK drain is incomplete')
    if not retain:
        return
    names = (prepared.REQUEST, prepared.PLAN, prepared.PREPARED,
             'go-supervisor-start.json', 'go-application-start.json', 'go-execution.json', 'process.json')
    if not plain:
        names += ('config.json', 'health.json', 'go-metric-finalization.json')
        state = {'schema_version': 1, 'pid': process.pid, 'collection': True,
                 'emission': True, 'effective_emission_off': False}
        expected = {'schema_version': 1, 'application_pid': process.pid, 'restored': state, 'flushed': state}
        if (not prepared.same(prepared.read(runtime / 'go-metric-finalization.json'), expected)
                or not prepared.same(result['sdk_health']['controls'], state)):
            raise ValueError('Go benchmark did not restore and flush collected metrics')
    process.evidence = {name: identity(runtime / name) for name in names}


def shutdown(owner: subprocess.Popen[str], process: Application | None,
             failure: BaseException | None) -> None:
    """Always reap the owned group and attempt both pipe closes after a body error."""
    try:
        try:
            if process is not None:
                benchmark.stop(process)
        finally:
            stop_group(owner)
    except BaseException as error:
        if failure is None:
            raise
        failure.add_note('Go benchmark cleanup also failed: ' + type(error).__name__)
    finally:
        try:
            if owner.stdin is not None:
                owner.stdin.close()
        finally:
            if owner.stdout is not None:
                owner.stdout.close()


@contextmanager
def application(policy: Policy, runtime: Path, build: Path, *, plain: bool) -> Iterator[Application]:
    """Own pipes and the complete supervisor session, including failed measurement."""
    prepared.prepare(policy, runtime, build_directory=build, plain=plain)
    command = prepared.command(runtime)
    with (runtime.parent / ('plain.stderr' if plain else 'instrumented.stderr')).open('w') as errors:
        created = time.monotonic_ns()
        owner = subprocess.Popen(command, start_new_session=True, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=errors, text=True)
        process = None
        failure = None
        try:
            process = started(owner, runtime, created, plain=plain)
            files.write(runtime / 'process.json', {'driver_pid': os.getpid(), 'supervisor_pid': owner.pid,
                'application_pid': process.pid, 'sdk_pid': None if plain else process.pid,
                'supervisor_command': command})
            yield process
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                shutdown(owner, process, failure)
            except BaseException as error:
                if failure is None:
                    failure = error
                    raise
                failure.add_note('Go benchmark cleanup also failed: ' + type(error).__name__)
            finally:
                if process is not None:
                    try:
                        completed(process, runtime, plain=plain, retain=failure is None)
                    except BaseException as error:
                        if failure is None:
                            raise
                        failure.add_note('Go benchmark final validation also failed: ' + type(error).__name__)


def switch(endpoint: Path, process: Application, mode: str) -> dict[str, Any]:
    """The Go emission command returns only after the old export barrier settles."""
    collection, emission = benchmark.STATES[mode]
    control.request(endpoint, 'collection ' + ('on' if collection else 'off'))
    state = control.request(endpoint, 'emission ' + ('on' if emission else 'off'))
    if (type(state.get('pid')) is not int or state['pid'] != process.pid
            or state.get('collection') is not collection or state.get('emission') is not emission
            or state.get('effective_emission_off') is not (not emission)):
        raise ValueError('Go benchmark live process or metric state changed')
    return state


def measure(policy: Policy, output: Path, build: Path, lane: str,
            records: benchmark.Measurements) -> Application:
    """Time the ordinary body; observe embedded SDK resources once with the app."""
    plain = lane == 'plain'
    runtime = output / lane
    endpoint = None if plain else runtime / 'control.json'
    expected = benchmark.checksum(policy.data['benchmark']['iterations'])
    with application(policy, runtime, build, plain=plain) as process:
        if endpoint is not None:
            control.wait_ready(endpoint, process.pid)
            switch(endpoint, process, 'both_off')
        response = benchmark.batch(process, 1)
        process.first_response = {'elapsed_ns': time.monotonic_ns() - process.created_at_monotonic_ns,
            'calls': 1, 'response': response,
            'scope': 'supervisor creation through first workload response; includes verification, SDK readiness, controls, IPC and body'}
        files.write(runtime / 'go-first-response.json', process.first_response)
        if response['checksum'] != benchmark.checksum(1):
            raise ValueError('Go benchmark startup checksum mismatch')
        configuration = policy.data['benchmark']
        for _ in range(configuration['warmup']):
            if benchmark.batch(process, configuration['iterations'])['checksum'] != expected:
                raise ValueError('Go benchmark warmup checksum mismatch')
        if endpoint is not None:
            control.request(endpoint, 'flush')
        for sample in range(configuration['samples']):
            for mode in benchmark_process.state_order(sample, plain=plain):
                if endpoint is not None:
                    records.transitions.append({'sample': sample, 'mode': mode, **switch(endpoint, process, mode)})
                # There is no separate SDK process. A second role with the same
                # PID would count the same CPU and RSS twice in comparisons.
                result = benchmark_process.measured_batch(process, configuration['iterations'], endpoint,
                    None, output, sample, mode, supervisor_pid=process.owner.pid)
                with (output / 'raw.jsonl').open('a') as raw:
                    raw.write(json.dumps({**result, 'sample': sample, 'mode': mode, 'pid': process.pid}) + '\n')
                if result['checksum'] != expected:
                    raise ValueError('Go plain/instrumented workload checksum differs')
                records.samples[mode].append(result['elapsed_ns'] / configuration['iterations'])
        if endpoint is not None:
            # The balanced schedule can finish off with retained measurements
            # from its last collection-only batch. Restore and flush outside all
            # sample windows, before closing the SDK that owns those values.
            restored = switch(endpoint, process, 'both_on')
            flushed = control.request(endpoint, 'flush')
            files.write(runtime / 'go-metric-finalization.json', {'schema_version': 1,
                'application_pid': process.pid, 'restored': restored, 'flushed': flushed})
    process.evidence['go-first-response.json'] = identity(runtime / 'go-first-response.json')
    return process


def run(policy: Policy, output: Path, *, build_directory: Path,
        comparison_id: str | None = None, publish: bool = True) -> dict[str, Any]:
    """Use one immutable build and common policy for a stock and four-state run."""
    built = prepared.verify_build(build_directory, policy)
    receipt = built['receipt']
    if Path(receipt['root']) != prepared.ROOT / 'examples/go' or receipt['target'] != '.':
        raise ValueError('Go comparison requires the unchanged shared examples/go application')
    output = output.resolve()
    if (path_boundaries.within(output, Path(receipt['root']))
            or path_boundaries.within(output, build_directory)):
        raise ValueError('Go benchmark output must be outside original sources and retained build')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    records = benchmark.Measurements()
    processes = {lane: measure(policy, output, build_directory, lane, records)
                 for lane in ('plain', 'instrumented')}
    for lane, process in processes.items():
        for name, expected in process.evidence.items():
            if identity(output / lane / name) != expected:
                raise ValueError('Go retained benchmark evidence changed')
    if not prepared.same(prepared.verify_build(build_directory, policy), built):
        raise ValueError('Go benchmark build identity changed')
    toolchain = receipt['toolchain']
    source_digest = prepared.digest(receipt['launch_inputs'])
    run_identity = benchmark_process.RunIdentity('go', toolchain['go_version'],
        'gc; official otelc ' + toolchain['version'],
        ('stock original / private compile-time woven; -mod=readonly',
         'native toolchain; no changed application dependency versions'), source_digest)
    pids = {lane: process.pid for lane, process in processes.items()}
    report = benchmark_process.summary(policy, output, records, run_identity, pids,
                                       pids['instrumented'], comparison_id)
    report.update(launch_profile=prepared.PROFILE, source_complete=False,
        sdk_process_model='embedded; application CPU/RSS includes SDK; counted once',
        supervisor_pids={lane: process.owner.pid for lane, process in processes.items()},
        launch_to_first_response={lane: process.first_response for lane, process in processes.items()},
        launch_evidence={lane: process.evidence for lane, process in processes.items()},
        build_identity=built['identity'], toolchain=toolchain,
        config_sha256=prepared.digest(policy.data), downstream_receipt='unavailable',
        baseline={'plain': 'original stock Go binary under the public prepared supervisor; no probes or SDK',
                  'instrumented': 'private woven Go binary under the public prepared supervisor; embedded SDK'})
    files.write(output / 'report.json', report)
    if publish:
        benchmark.export_result(report, policy.data['export']['endpoint'])
    return report
