"""Retain a genuine Go benchmark through a pinned oteleq capture and local stack."""
from __future__ import annotations

import argparse
import gzip
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
from types import ModuleType
import urllib.request

from ci import go_benchmark_downstream as downstream
from ci import go_benchmark_receipt as receipt
from otelc import benchmark_cleanup, file_inputs, path_boundaries, prepared_go
from otelc.config import Policy
from otelc.python_sdk_io import decode

ROOT = Path(__file__).resolve().parents[1]
FLOOR = 8 * 1024**3


def encoded(value: object) -> bytes:
    """Retain finite original observations without a second serialization policy."""
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def git(root: Path, *arguments: str) -> bytes:
    """Read declared Git source identities without changing a checkout."""
    return subprocess.check_output(['git', '-C', str(root), *arguments])


def catalogue(root: Path, expected: str) -> dict:
    """Keep every tracked source and its original physical facts held."""
    receipt.require(git(root, 'rev-parse', 'HEAD').decode().strip() == expected
                    and not git(root, 'status', '--porcelain', '--untracked-files=no'),
                    'benchmark tools must be the declared clean source revision')
    names = git(root, 'ls-files', '-z').decode().split('\0')[:-1]
    return {name: source_file(root / name) for name in names}


def physical(value: os.stat_result) -> list[int]:
    """Access time can change on reads; preserve identity and all write facts."""
    return [value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns, value.st_uid, value.st_gid,
            value.st_nlink]


def source_file(path: Path) -> dict:
    """A replacement or changed/restored original is not the held source file."""
    before = path.lstat()
    receipt.require(stat.S_ISREG(before.st_mode), 'qualification source must be an ordinary file')
    value, _ = file_inputs.checked_file(path, nofollow=True)
    after = path.lstat()
    receipt.require(physical(before) == physical(after), 'qualification source changed while being read')
    return value | {'physical_facts': physical(after)}


def execute(arguments: list[str], output: Path, label: str) -> None:
    """Retain the real command and request graceful driver cleanup on timeout."""
    receipt.require(shutil.disk_usage(output).free >= FLOOR,
                    'benchmark builds and applications require at least 8 GiB free')
    environment = os.environ | {'OTELC_PYTHON': sys.executable, 'PYTHONDONTWRITEBYTECODE': '1'}
    started = time.monotonic_ns()
    with (output / (label + '.stdout')).open('xb') as stdout, \
            (output / (label + '.stderr')).open('xb') as stderr:
        process = subprocess.Popen(arguments, cwd=ROOT, env=environment, stdout=stdout, stderr=stderr)
        failure = None
        try:
            code = process.wait(timeout=900)
            if code != 0:
                raise subprocess.CalledProcessError(code, arguments)
        except BaseException as error:
            failure = error
            if process.poll() is None:
                cleanup = benchmark_cleanup.Cleanup(error)
                cleanup.run(lambda: process.send_signal(signal.SIGINT), 'Qualification driver interrupt')
                cleanup.run(lambda: process.wait(timeout=30), 'Qualification driver graceful stop')
                if process.poll() is None:
                    cleanup.run(process.kill, 'Qualification driver forced stop')
                    cleanup.run(process.wait, 'Qualification driver reap')
            raise
        finally:
            cleanup = benchmark_cleanup.Cleanup(failure)
            cleanup.run(lambda: downstream.save(output, label + '.command.json', encoded({
                'arguments': arguments, 'driver_pid': process.pid,
                'returncode': process.returncode, 'started_monotonic_ns': started,
                'finished_monotonic_ns': time.monotonic_ns()})), 'Qualification command evidence')
            cleanup.finish()


def policy(endpoint: str, output: Path) -> Policy:
    """Use the common metrics-only policy, initially off, in both actual lanes."""
    text = (ROOT / 'examples/benchmark-metrics.toml').read_text()
    for before, after in (('endpoint = "http://127.0.0.1:4320"', 'endpoint = ' + json.dumps(endpoint)),
                          ('collection = true', 'collection = false'),
                          ('emission = true', 'emission = false')):
        receipt.require(text.count(before) == 1, 'common benchmark policy template changed')
        text = text.replace(before, after, 1)
    downstream.save(output.parent, output.name, text.encode())
    return Policy.load(output)


def ready() -> None:
    """Wait for stack readiness separately from proof of benchmark receipt."""
    deadline = time.monotonic() + 60
    while True:
        try:
            for url in ('http://127.0.0.1:9091/-/ready', 'http://127.0.0.1:3001/api/health'):
                with downstream.open_local(url, timeout=2) as response:
                    receipt.require(response.status == 200, 'benchmark stack is not ready')
            request = urllib.request.Request('http://127.0.0.1:4320/v1/metrics', data=b'',
                                            headers={'Content-Type': 'application/x-protobuf'})
            with downstream.open_local(request, timeout=2) as response:
                receipt.require(response.status == 200, 'benchmark Collector is not ready')
            return
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(.5)


def records(comparison: Path, evidence: Path, value: dict) -> None:
    """Retain fixed completed records; exclude live control authentication files."""
    common = {prepared_go.REQUEST, prepared_go.PLAN, prepared_go.PREPARED,
              'go-supervisor-start.json', 'go-application-start.json',
              'go-execution.json', 'process.json', 'go-first-response.json'}
    for lane in ('plain', 'instrumented'):
        expected = common | ({'config.json', 'health.json', 'go-metric-finalization.json'}
                             if lane == 'instrumented' else set())
        held = value['launch_evidence'][lane]
        receipt.require(set(held) == expected, 'Go retained launch record denominator changed')
        for name in sorted(expected):
            path = comparison / lane / name
            before = file_inputs.identity(path)
            receipt.require(prepared_go.same(before, held[name]), 'Go original launch evidence changed')
            raw = path.read_bytes()
            receipt.require(file_inputs.identity(path) == before, 'Go launch evidence changed during retention')
            downstream.save(evidence, lane + '-' + name, raw)
    for name in ('report.json', 'raw.jsonl', 'responses.jsonl'):
        downstream.save(evidence, name, (comparison / name).read_bytes())


def products(build: Path, evidence: Path) -> None:
    """Keep compiled images and declared private source bytes, without caches."""
    downstream.save(evidence, 'go-build.json', (build / 'build.json').read_bytes())
    downstream.save(evidence, 'go-inventory.json', (build / 'inspection/inventory.json').read_bytes())
    for lane in ('plain', 'instrumented'):
        with (build / lane).open('rb') as source, (evidence / (lane + '-image.gz')).open('xb') as target:
            with gzip.GzipFile(fileobj=target, mode='wb', mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
    # These are the source-only trees named in the build record. Compiler and
    # weaver caches live elsewhere and are not part of the retained selection.
    with tarfile.open(evidence / 'private-inputs.tar.gz', 'w:gz') as archive:
        for folder in ('private', 'sdk', 'context'):
            archive.add(build / folder, arcname=folder, recursive=True)


def perform(capture: ModuleType, output: Path, evidence: Path, comparison_id: str) -> dict:
    """Build once, measure, stop the capture and check retained downstream data."""
    ready()
    with capture.forwarding_receiver('http://127.0.0.1:4320', evidence / 'wire',
            max_requests=4096, max_bytes=32 * 1024**2, allow_chunked=True) as received:
        configuration = output / 'otelc.toml'
        selected = policy(received.endpoint, configuration)
        downstream.save(evidence, 'otelc.toml', configuration.read_bytes())
        cli = [str(ROOT / 'bin/quux-otelc'), '--config', str(configuration)]
        build, comparison = output / 'go-build', output / 'comparison'
        execute([*cli, 'go-build', '--scope-safe', '--root', str(ROOT / 'examples/go'),
                 '--output', str(build)], evidence, 'build')
        built = prepared_go.verify_build(build, selected)
        receipt.workload_selection(built)
        execute([*cli, 'benchmark', '--language', 'go', '--build', str(build),
                 '--comparison-id', comparison_id, '--output', str(comparison)], evidence, 'benchmark')
    received.require_complete()
    value = decode((comparison / 'report.json').read_bytes())
    raw = [decode(line.encode()) for line in (comparison / 'raw.jsonl').read_text().splitlines()]
    responses = [decode(line.encode()) for line in (comparison / 'responses.jsonl').read_text().splitlines()]
    retained = receipt.report(value, raw, responses, selected, comparison_id)
    records(comparison, evidence, value)
    instrumentation = receipt.instrumentation(value, decode((evidence / 'instrumented-health.json').read_bytes()),
        decode((evidence / 'instrumented-config.json').read_bytes()), built)
    telemetry = receipt.workload_telemetry(received.receipt,
        lambda name: (evidence / 'wire' / name).read_bytes(),
        service=selected.data['resource']['service_name'], function=instrumentation['function'],
        calls=instrumentation['collected_calls'])
    products(build, evidence)
    receipt.require(prepared_go.same(prepared_go.verify_build(build, selected), built),
                    'Go build inputs or images changed during retention')
    observed = downstream.check(value, evidence / 'downstream')
    return {'status': 'pass for retained Go benchmark and downstream mean data',
              'retained': retained, 'downstream': observed,
              'instrumentation': instrumentation, 'workload_telemetry': telemetry,
              'limits': 'One host; lifetimes off and tracing fixed by the common policy. No whole-language/source-closure, trace/lifetime completeness, visual dashboard or oteleq provider admission'}


def unchanged(root: Path, revision: str, before: dict) -> None:
    """A changed source catalogue fails both successful and failed runs."""
    receipt.require(catalogue(root, revision) == before, 'benchmark original source facts changed')


def destination(output: Path, oteleq: Path) -> Path:
    """Allow a new external output or the SDK's fixed ignored build directory."""
    output = output.resolve()
    receipt.require(not output.exists(), 'qualification requires a new output directory')
    build = ROOT / 'build'
    within_build = (path_boundaries.within(output, build) if build.exists()
                    else output.is_relative_to(build))
    receipt.require((not path_boundaries.within(output, ROOT) or within_build)
                    and not path_boundaries.within(output, oteleq),
                    'qualification output overlaps original inputs')
    return output


def run(oteleq: Path, expected: str, output: Path, comparison_id: str) -> dict:
    """Use the public CLI, unchanged example and a separately pinned capture."""
    receipt.require(re.fullmatch('[a-f0-9]{40}', expected) is not None
                    and re.fullmatch('[a-z0-9_-]{1,128}', comparison_id) is not None,
                    'qualification requires an immutable capture revision and unambiguous run ID')
    oteleq = oteleq.resolve(strict=True)
    sdk_head = git(ROOT, 'rev-parse', 'HEAD').decode().strip()
    sdk_before, capture_before = catalogue(ROOT, sdk_head), catalogue(oteleq, expected)
    output = destination(output, oteleq)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = output / 'evidence'
    evidence.mkdir(mode=0o700)
    failure = None
    try:
        downstream.save(evidence, 'source-identities.json', encoded({
            'otelc': {'head': sdk_head, 'files': sdk_before},
            'oteleq': {'head': expected, 'files': capture_before},
            'interpreter_spelling': sys.executable,
            'interpreter_target': file_inputs.identity(Path(sys.executable).resolve(strict=True))}))
        for root, name, revision in ((ROOT, 'otelc-source.tar.gz', sdk_head),
                                      (oteleq, 'oteleq-source.tar.gz', expected)):
            subprocess.run(['git', '-C', str(root), 'archive', '--format=tar.gz',
                            '--output=' + str(evidence / name), revision], check=True)
        sys.path.insert(0, str(oteleq / 'examples'))
        capture = importlib.import_module('otelc_capture_forward')
        receipt.require(Path(capture.__file__).resolve() == oteleq / 'examples/otelc_capture_forward.py',
                        'benchmark capture module is not from the declared source')
        result = perform(capture, output, evidence, comparison_id)
    except BaseException as error:
        failure = error
        raise
    finally:
        cleanup = benchmark_cleanup.Cleanup(failure)
        cleanup.run(lambda: unchanged(ROOT, sdk_head, sdk_before), 'Qualification otelc source verification')
        cleanup.run(lambda: unchanged(oteleq, expected, capture_before), 'Qualification oteleq source verification')
        cleanup.finish()
    result.update(otelc_head=sdk_head, oteleq_head=expected)
    downstream.save(evidence, 'qualification.json', encoded(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--oteleq-root', type=Path, required=True)
    parser.add_argument('--oteleq-sha', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--comparison-id', required=True)
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.oteleq_root, arguments.oteleq_sha,
                         arguments.output, arguments.comparison_id), indent=2))


if __name__ == '__main__':
    main()
