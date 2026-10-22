"""Retain source-launch inputs separately from any claim about action delivery."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any


def identity(filename: Path) -> dict[str, Any]:
    """Bind a regular file's bytes and reject a read that races a mutation."""
    filename = filename.absolute()
    descriptor = os.open(filename, os.O_RDONLY | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f'launch input is not a regular file: {filename}')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if stable(before) != stable(after) or stable(after) != stable(filename.stat()):
        raise ValueError(f'launch input changed while reading: {filename}')
    result = {'path': str(filename), 'resolved_path': str(filename.resolve(strict=True)),
              'sha256': digest, 'size': after.st_size}
    if filename.is_symlink():
        result['symlink'] = os.readlink(filename)
    return result


def inventory(directory: Path) -> list[dict[str, Any]]:
    """Inventory complete producer directories without silently skipping errors."""
    if not directory.is_dir():
        raise ValueError(f'launch tooling directory is missing: {directory}')
    records = []
    for current, directories, files in os.walk(directory, onerror=raise_walk_error):
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise ValueError('symlinked producer directories are unsupported')
        records.extend(identity(Path(current) / name) for name in sorted(files))
    return sorted(records, key=lambda item: item['path'])


def raise_walk_error(error: OSError) -> None:
    """An unreadable tooling subtree is an incomplete inventory, not an empty one."""
    raise error


def package_context(application: Path) -> list[dict[str, Any]]:
    """Bind the nearest package and all nearer absences that affect Node format."""
    records = []
    directory = application.parent
    while True:
        filename = directory / 'package.json'
        if os.path.lexists(filename):
            records.append(identity(filename))
            return records
        records.append({'path': str(filename), 'absent': True})
        if directory.parent == directory:
            return records
        directory = directory.parent


def capture(producer: Path, application: Path, selected: str, plan: dict[str, Any],
            generated: list[Path], argv: list[str]) -> dict[str, Any]:
    """Capture the actual installed closure, including optional platform packages."""
    environment = {key: value for key, value in os.environ.items() if key not in ('NODE_OPTIONS', 'NODE_PATH')}
    runtime = json.loads(subprocess.check_output([selected, '-p',
        'JSON.stringify({version:process.versions.node,abi:process.versions.modules,executable:process.execPath,architecture:process.arch,platform:process.platform})'],
        text=True, env=environment))
    tooling = {}
    if not (plan['plain'] and plan['language'] == 'javascript'):
        tooling['adapter'] = inventory(producer / 'adapters/node/src')
        tooling['packages'] = inventory(producer / 'node_modules')
        tooling['package_metadata'] = [identity(producer / name) for name in ('package.json', 'package-lock.json')]
        if not plan['plain']:
            tooling['observer'] = [identity(producer / 'build/tools' / f"otelc_node_observer-{runtime['version']}.node")]
        if not plan['plain'] and plan['context']['http']:
            manifest = producer / 'build/tools/otelc-http.json'
            product = json.loads(manifest.read_text())
            tooling['http'] = [identity(manifest), *inventory(producer / 'build/tools' / product['directory'])]
    return {'schema_version': 1, 'launch_id': plan['launch_id'], 'language': plan['language'],
        'backend': plan['backend'], 'plain': plan['plain'], 'root': plan['runtime']['root'],
        'argv': argv, 'cwd': str(Path.cwd().resolve()), 'launcher_pid': os.getpid(),
        'node': identity(Path(runtime.pop('executable')).resolve(strict=True)), 'runtime': runtime,
        'entry': identity(application), 'plan_sha256': hashlib.sha256(
            json.dumps(plan, sort_keys=True).encode()).hexdigest(),
        'resolved_plan': plan,
        'ambient_sha256': {key: hashlib.sha256(os.environ[key].encode()).hexdigest()
            for key in ('NODE_OPTIONS', 'NODE_PATH') if key in os.environ},
        'tooling': tooling, 'generated': [identity(filename) for filename in generated],
        'entry_context': package_context(application)}


def verify_tooling(expected: dict[str, Any]) -> None:
    """Recheck file sets as well as bytes, including dependency additions/removals."""
    producer = Path(__file__).resolve().parent.parent
    for group, items in expected['tooling'].items():
        if group in ('adapter', 'packages'):
            directory = producer / ('adapters/node/src' if group == 'adapter' else 'node_modules')
            actual = inventory(directory)
        elif group == 'http':
            product = json.loads(Path(items[0]['path']).read_text())
            actual = [identity(Path(items[0]['path'])), *inventory(producer / 'build/tools' / product['directory'])]
        else:
            actual = [identity(Path(item['path'])) for item in items]
        if actual != items:
            raise ValueError('launch tooling changed: ' + group)


def verify_emission(runtime: Path, prefix: str, record: dict[str, Any],
                    sources: set[tuple[str, str]]) -> None:
    """Validate an output against a preceding original source and retained bytes."""
    if (record['path'], record['source_sha256']) not in sources:
        raise ValueError('emitted module has no original source binding')
    output_path = runtime / (prefix + '-module-' + record['sha256'] + '.txt')
    if record['output_file'] != str(output_path):
        raise ValueError('emitted output is outside the generated launch namespace')
    output = identity(output_path)
    if output['sha256'] != record['sha256'] or output['size'] != record['size']:
        raise ValueError('retained emitted module changed')
    if 'map_file' in record:
        map_path = runtime / (prefix + '-module-' + record['map_sha256'] + '.txt')
        if record['map_file'] != str(map_path) or identity(map_path)['sha256'] != record['map_sha256']:
            raise ValueError('retained source map changed')


def loaded_inputs(runtime: Path, prefix: str) -> list[dict[str, Any]]:
    """Reject unknown or truncated records and changed observed inputs."""
    records = []
    sources: set[tuple[str, str]] = set()
    for line in (runtime / (prefix + '-sources.jsonl')).read_text().splitlines():
        record = json.loads(line)
        if not isinstance(record, dict) or record.get('kind') not in (
                'source', 'compiler_config', 'absent_config', 'package_context', 'absent_package',
                'compiled', 'instrumented', 'loaded'):
            raise ValueError('invalid loaded input record')
        if record['kind'] in ('compiled', 'instrumented', 'loaded'):
            verify_emission(runtime, prefix, record, sources)
        else:
            verify_read_input(record)
        if record['kind'] == 'source':
            sources.add((record['path'], record['sha256']))
        records.append(record)
    return records


def verify_read_input(record: dict[str, Any]) -> None:
    """Recheck original/configuration bytes or the recorded absence of a file."""
    if record['kind'] in ('absent_config', 'absent_package'):
        if os.path.lexists(record['path']):
            raise ValueError('previously absent input changed: ' + record['path'])
        return
    if str(Path(record['requested_path']).resolve(strict=True)) != record['path']:
        raise ValueError('input resolution changed: ' + record['requested_path'])
    current = identity(Path(record['path']))
    if any(current.get(key) != record[key] for key in ('path', 'sha256', 'size')):
        raise ValueError('loaded source or compiler config changed: ' + record['path'])


def verify(runtime: Path, expected: dict[str, Any]) -> dict[str, Any]:
    """Check retained launch/source evidence after application and SDK shutdown."""
    runtime = runtime.resolve(strict=True)
    prefix = 'node-plain' if expected['plain'] else 'node'
    filename = runtime / (prefix + '-launch.json')
    if json.loads(filename.read_text()) != expected:
        raise ValueError('launch manifest changed')
    if package_context(Path(expected['entry']['path'])) != expected['entry_context']:
        raise ValueError('entry package context changed')
    for item in [expected['node'], expected['entry'], *expected['generated']]:
        if identity(Path(item['path'])) != item:
            raise ValueError('launch input changed: ' + item['path'])
    verify_tooling(expected)
    records = []
    if not (expected['plain'] and expected['language'] == 'javascript'):
        records = loaded_inputs(runtime, prefix)
        if not any(item['kind'] == 'source' and item['path'] == expected['entry']['path'] for item in records):
            raise ValueError('launch entry is missing from loaded source evidence')
    result = {'schema_version': 1, 'launch_id': expected['launch_id'], 'phase': 'verified',
        'action_delivery_verified': False, 'loaded_inputs': records, 'launch_manifest': identity(filename)}
    if not expected['plain']:
        sdk_report = runtime / 'node-status.json'
        sdk = json.loads(sdk_report.read_text())
        if sdk['launch_id'] != expected['launch_id']:
            raise ValueError('SDK report belongs to another launch')
        result.update(application_pid=sdk['pid'], sdk_pid=sdk['sdk_pid'], sdk_report=identity(sdk_report))
    return result


def finish(runtime: Path, expected: dict[str, Any]) -> bool:
    """Retain a failed verification as evidence and never publish it as success."""
    try:
        report = verify(runtime, expected)
    except (ValueError, OSError, KeyError) as error:
        report = {'schema_version': 1, 'launch_id': expected['launch_id'], 'phase': 'failed',
            'action_delivery_verified': False, 'error': str(error)}
    prefix = 'node-plain' if expected['plain'] else 'node'
    descriptor = os.open(runtime / (prefix + '-inputs.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(report, output, sort_keys=True)
        output.write('\n')
    return report['phase'] == 'verified'
