"""An ordinary CPython benchmark process, without otelc runtime observation."""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from otelc import benchmark, prepared_python as prepared
from otelc import python_benchmark_launch as launch
from otelc import python_launch_files as files
from otelc import python_recipe as recipe
from otelc.config import Policy
from otelc.file_inputs import identity

ROOT = Path(__file__).resolve().parents[1]
REQUEST = 'python-stock-request.json'


def inputs(policy: Policy, runtime: Path) -> dict[str, Any]:
    """Bind ordinary launch intent and maintained tooling, without building a driver."""
    original = recipe.request(policy, runtime, root=ROOT,
        entry=ROOT / 'examples/python/BenchmarkApp.py', module=None, plain=True)
    plan = recipe.resolve_plan(original)
    recipe.verify(original, plan)
    maintained = [ROOT / 'bin/quux-otelc', *sorted((ROOT / 'otelc').glob('*.py')),
                  ROOT / 'requirements.txt', ROOT / 'requirements-dev.txt']
    return {'schema_version': 1, 'kind': 'otelc.python.stock-benchmark.request',
            'request': original, 'plan': plan,
            'source': identity(ROOT / 'examples/python/BenchmarkApp.py'),
            'producer': [identity(path) for path in maintained],
            'source_complete': False}


def verify(value: dict[str, Any], runtime: Path) -> None:
    """Recheck retained inputs; filesystem identity is not consumed-source evidence."""
    retained, _ = files.read(runtime / REQUEST)
    if retained != value:
        raise ValueError('stock Python benchmark retained evidence changed')
    recipe.verify(value['request'], value['plan'])
    for recorded in [value['source'], *value['producer']]:
        if identity(Path(recorded['path'])) != recorded:
            raise ValueError('stock Python benchmark input changed')


def recheck(process: launch.Application) -> None:
    """The other lane cannot change the completed stock lane's retained inputs."""
    verify(process.start['inputs'], Path(process.start['runtime']))
    launch.recheck(process)


def stop(process: launch.Application) -> None:
    """Reap the directly owned PID even when cooperative stop or group cleanup fails."""
    failure = None
    try:
        benchmark.stop(process)
    except BaseException as error:
        failure = error
    finally:
        for stream in (process.owner.stdin, process.owner.stdout):
            if stream is not None:
                try:
                    stream.close()
                except BaseException as error:
                    if failure is None:
                        failure = error
                    else:
                        failure.add_note('Stock pipe cleanup also failed: ' + type(error).__name__)
        try:
            if process.owner.poll() is None:
                process.owner.terminate()
            try:
                process.owner.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.owner.kill()
                process.owner.wait(timeout=3)
        except BaseException as error:
            if failure is None:
                failure = error
            else:
                failure.add_note('Direct stock process cleanup also failed: ' + type(error).__name__)
        try:
            prepared.stop_group(process.owner)
        except BaseException as error:
            if failure is None:
                failure = error
            else:
                failure.add_note('Stock process-group cleanup also failed: ' + type(error).__name__)
    if failure is not None:
        raise failure


@contextmanager
def application(policy: Policy, runtime: Path) -> Iterator[launch.Application]:
    """Own one direct interpreter through completion, without a separate supervisor or SDK."""
    runtime.mkdir(mode=0o700)
    runtime = files.private_directory(runtime)
    original = inputs(policy, runtime)
    files.write(runtime / REQUEST, original)
    plan = original['plan']
    command = [plan['python'], *plan['native_arguments']]
    with (runtime.parent / 'plain.stderr').open('w') as errors:
        created = time.monotonic_ns()
        owner = subprocess.Popen(command, cwd=plan['cwd'], start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
        start = {'schema_version': 1, 'kind': 'otelc.python.stock-benchmark.start',
                 'driver_pid': os.getpid(), 'application_pid': owner.pid,
                 'supervisor_pid': None, 'sdk_pid': None, 'process_group': owner.pid,
                 'command': command, 'runtime': str(runtime), 'inputs': original,
                 'created_at_monotonic_ns': created}
        process = launch.Application(owner, owner.pid, None, start, stock=True,
                                     created_at_monotonic_ns=created)
        failure = None
        try:
            files.write(runtime / 'python-application-start.json', start)
            yield process
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                stop(process)
            except BaseException as error:
                if failure is None:
                    raise
                failure.add_note('Stock benchmark cleanup also failed: ' + type(error).__name__)
                for note in getattr(error, '__notes__', ()):
                    failure.add_note(note)
        verify(original, runtime)
        files.write(runtime / 'python-execution.json', {
            'schema_version': 1, 'kind': 'otelc.python.stock-benchmark.execution',
            'phase': 'closed', 'application': {'pid': owner.pid, 'returncode': owner.returncode},
            'driver_pid': os.getpid(), 'supervisor_pid': None, 'sdk': None,
            'input_recheck': True, 'source_complete': False,
            'entry_evidence': {'phase': 'unavailable', 'reason': 'stock interpreter has no input observer'},
            'first_response': process.first_response})
        process.evidence = {name: identity(runtime / name) for name in
                            (REQUEST, 'python-application-start.json', 'python-execution.json')}
