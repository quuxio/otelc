"""Deterministic source-launch recipe checked against independently supplied intent."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from otelc.config import Policy
from otelc.file_inputs import checked_file

ENVIRONMENT_KEYS = ('NODE_OPTIONS', 'NODE_PATH', 'NODE_PRESERVE_SYMLINKS', 'NODE_EXTRA_CA_CERTS',
                    'NODE_ICU_DATA', 'NODE_COMPILE_CACHE', 'NODE_DISABLE_COMPILE_CACHE',
                    'NODE_COMPILE_CACHE_READONLY', 'UV_THREADPOOL_SIZE', 'TZ', 'LANG', 'LC_ALL',
                    'LC_CTYPE', 'TMPDIR', 'TEMP', 'TMP')
TOOLING_REMOVED_PREFIXES = ('NODE_', 'OTEL_', 'OTELC_')


def encoded(value: Any) -> str:
    """Use the same deterministic JSON encoding for generated files and their recipe."""
    return json.dumps(value, sort_keys=True) + '\n'


def environment_record(value: str | None) -> dict[str, str]:
    """An unset variable differs from an explicitly empty value."""
    if value is None:
        return {'state': 'absent'}
    return {'state': 'present', 'value': value, 'sha256': hashlib.sha256(value.encode()).hexdigest()}


def environment_values(records: dict[str, Any]) -> dict[str, str | None]:
    """Require the complete allowlist and exact value/hash pairs without exposing values."""
    if set(records) != set(ENVIRONMENT_KEYS):
        raise ValueError('invalid Node launch environment key set')
    values = {}
    for key, item in records.items():
        if not isinstance(item, dict) or item.get('state') not in ('absent', 'present'):
            raise ValueError('invalid Node launch environment state: ' + key)
        value = item.get('value') if item['state'] == 'present' else None
        if item['state'] == 'present' and not isinstance(value, str) or item != environment_record(value):
            raise ValueError('invalid Node launch environment value/hash: ' + key)
        values[key] = value
    return values


def tooling_environment() -> dict[str, str]:
    """Metadata inspection must not run application Node/preload/profiler settings."""
    return {key: value for key, value in os.environ.items() if not key.startswith(TOOLING_REMOVED_PREFIXES)}


def environment_snapshot(environment: dict[str, str]) -> dict[str, Any]:
    """Retain only declared launch inputs, never an unrestricted environment dump."""
    return {key: environment_record(environment.get(key)) for key in ENVIRONMENT_KEYS}


def request(policy: Policy, runtime: Path, language: str, *, root: Path, entry: Path,
            selected: str, backend: str, plain: bool, gc_fixture: bool,
            arguments: tuple[str, ...]) -> dict[str, Any]:
    """Keep original caller paths and policy separately from generated tool settings."""
    return {'schema_version': 1, 'profile': 'node-source-v1', 'policy': policy.data,
            'policy_source': policy.source,
            'language': language, 'backend': backend, 'plain': plain, 'gc_fixture': gc_fixture,
            'cwd': str(Path.cwd().resolve()), 'node': selected, 'arguments': list(arguments),
            'paths': {name: {'requested': str(path), 'resolved': str(path.resolve())}
                      for name, path in (('root', root), ('entry', entry), ('runtime', runtime))},
            'environment': environment_snapshot(os.environ)}


def resolve_plan(original: dict[str, Any], launch_id: str) -> dict[str, Any]:
    """Translate only common policy and the fixed adapter profile into a source plan."""
    fields = {'schema_version', 'profile', 'policy', 'policy_source', 'language', 'backend',
              'plain', 'gc_fixture', 'cwd', 'node', 'arguments', 'paths', 'environment'}
    if set(original) != fields:
        raise ValueError('invalid Node source launch request fields')
    policy = Policy.parse(original['policy'])
    if (type(original['schema_version']) is not int or original['schema_version'] != 1
            or original['profile'] != 'node-source-v1'
            or original['language'] not in ('javascript', 'typescript')
            or original['language'] not in policy.data['languages']
            or original['backend'] not in ('native', 'api')
            or type(original['plain']) is not bool or type(original['gc_fixture']) is not bool
            or not re.fullmatch('[a-f0-9]{32}', launch_id)
            or not isinstance(original['arguments'], list)
            or any(not isinstance(value, str) for value in original['arguments'])):
        raise ValueError('invalid Node source launch request')
    environment_values(original['environment'])
    return {**policy.data, 'language': original['language'], 'backend': original['backend'],
            'plain': original['plain'], 'launch_id': launch_id,
            'annotations': {'read_existing': False, 'inject_generated': False},
            'source_matchers': policy.data['sources'], 'function_matchers': policy.data['functions'],
            'lifetime_matchers': {key: policy.data['lifetimes'][key] for key in ('include', 'exclude')},
            'runtime': {'root': original['paths']['root']['resolved'],
                        'directory': original['paths']['runtime']['resolved'],
                        'max_functions': 4096, 'max_active_calls': 4096}}


def bootstrap_text(producer: Path, runtime: Path, *, plain: bool) -> str:
    """Generate the sole allowed bootstrap; no code comes from a recorded command."""
    prefix = 'node-plain' if plain else 'node'
    target = producer / 'adapters/node/src' / ('plain-source-loader.ts' if plain else 'source-bootstrap.ts')
    return ('process.env.OTELC_NODE_PLAN = ' + json.dumps(str(runtime / (prefix + '-plan.json'))) + ';\n'
            'require(' + json.dumps(str(target)) + ');\n')


def invocation(original: dict[str, Any]) -> list[str]:
    """Derive argv without executing or accepting an arbitrary recorded command."""
    runtime = Path(original['paths']['runtime']['resolved'])
    prefix = 'node-plain' if original['plain'] else 'node'
    preload = not (original['plain'] and original['language'] == 'javascript')
    return [original['node'], '--enable-source-maps', *(['--expose-gc'] if original['gc_fixture'] else []),
            *(['--require', str(runtime / (prefix + '-bootstrap.cjs'))] if preload else []),
            original['paths']['entry']['resolved'], *original['arguments']]


def verify_paths(original: dict[str, Any], runtime: Path) -> None:
    """Re-resolve original caller paths from the recorded working directory."""
    cwd = Path(original['cwd'])
    if not cwd.is_absolute() or cwd.resolve(strict=True) != cwd:
        raise ValueError('Node request has an invalid working directory')
    if set(original['paths']) != {'root', 'entry', 'runtime'}:
        raise ValueError('Node request has an invalid path set')
    for name, item in original['paths'].items():
        selected = Path(item['requested'])
        resolved = (selected if selected.is_absolute() else cwd / selected).resolve(strict=True)
        if str(resolved) != item['resolved'] or name == 'runtime' and resolved != runtime:
            raise ValueError('Node requested path resolution changed: ' + name)


def verify_startup(records: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    """Bind actual process inputs at the source-loader boundary before the entry executes."""
    starts = [item for item in records if item['kind'] == 'startup']
    if len(starts) != 1 or not records or records[0] != starts[0]:
        raise ValueError('Node startup receipt is missing, duplicated or out of order')
    actual = starts[0]['startup']
    original = manifest['request']
    argv = invocation(original)
    entry_index = len(argv) - len(original['arguments']) - 1
    if (starts[0]['path'] != manifest['entry']['path'] or type(actual['pid']) is not int or actual['pid'] <= 0
            or actual['node'] != original['node'] or actual['cwd'] != original['cwd']
            or actual['argv'] != [argv[0], *argv[entry_index:]] or actual['exec_argv'] != argv[1:entry_index]
            or actual['environment'] != original['environment']):
        raise ValueError('Node effective startup inputs differ from the request')


def verify_policy_source(original: dict[str, Any]) -> None:
    """The retained TOML must be the same read that produced the common policy."""
    source = original['policy_source']
    if source is None:
        return
    metadata, content = checked_file(Path(source['path']), include_content=True)
    if (metadata != {key: value for key, value in source.items() if key != 'text'}
            or content.decode() != source['text']
            or Policy.parse(tomllib.loads(source['text'])).data != original['policy']):
        raise ValueError('Node requested policy source changed')


def verify_generated(runtime: Path, original: dict[str, Any], manifest: dict[str, Any],
                     plan: dict[str, Any], producer: Path) -> None:
    """Content hashes alone cannot authorise a different generated bootstrap or plan."""
    prefix = 'node-plain' if original['plain'] else 'node'
    files = {prefix + '-request.json': encoded(original)}
    if not (original['plain'] and original['language'] == 'javascript'):
        files[prefix + '-plan.json'] = encoded(plan)
        files[prefix + '-bootstrap.cjs'] = bootstrap_text(producer, runtime, plain=original['plain'])
    if {item['path'] for item in manifest['generated']} != {str(runtime / name) for name in files}:
        raise ValueError('Node generated file set differs from the recipe')
    for name, content in files.items():
        _, actual = checked_file(runtime / name, include_content=True, nofollow=True)
        if actual != content.encode():
            raise ValueError('Node generated request/plan/bootstrap differs from its fixed recipe: ' + name)


def verify(runtime: Path, original: dict[str, Any], manifest: dict[str, Any], *, producer: Path) -> None:
    """Validate fixed argv, plan and bootstrap against original intent; never run evidence."""
    runtime = runtime.resolve(strict=True)
    producer = producer.resolve(strict=True)
    plan = resolve_plan(original, manifest['launch_id'])
    verify_paths(original, runtime)
    verify_policy_source(original)
    if manifest['request'] != original or manifest['argv'] != invocation(original) or manifest['cwd'] != original['cwd']:
        raise ValueError('Node launch argv/request differs from the fixed recipe')
    if any(manifest[key] != original[key] for key in ('language', 'backend', 'plain')):
        raise ValueError('Node launch adapter differs from the request')
    if manifest['root'] != plan['runtime']['root'] or manifest['node']['path'] != original['node']:
        raise ValueError('Node launch root/runtime differs from the request')
    if manifest['entry']['path'] != original['paths']['entry']['resolved']:
        raise ValueError('Node launch entry differs from the request')
    if manifest['resolved_plan'] != plan or manifest['plan_sha256'] != hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest():
        raise ValueError('Node generated plan differs from the requested policy')
    values = environment_values(original['environment'])
    expected_environment = {key: hashlib.sha256(values[key].encode()).hexdigest()
                            for key in ('NODE_OPTIONS', 'NODE_PATH') if values[key] is not None}
    if manifest['ambient_sha256'] != expected_environment:
        raise ValueError('Node environment differs from the request')
    inspection = {key: environment_record(None if key.startswith(TOOLING_REMOVED_PREFIXES) else value)
                  for key, value in values.items()}
    if manifest['tooling_environment'] != inspection:
        raise ValueError('Node metadata environment differs from the isolated tooling recipe')
    verify_generated(runtime, original, manifest, plan, producer)
