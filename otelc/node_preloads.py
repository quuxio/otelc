"""Bind ambient preload entries using a separate, non-executing Node resolver."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from otelc.file_inputs import identity
from otelc.node_context import package_context

RESOLVER = Path(__file__).resolve().parent.parent / 'adapters/node/src/compiler/preload-resolver.mjs'
OPTIONS = {'--require': 'require', '-r': 'require', '--import': 'import',
           '--loader': 'loader', '--experimental-loader': 'loader'}


def finish_token(result: list[str], word: list[str]) -> None:
    """Node ignores empty quoted words and separates only on unquoted spaces."""
    if word:
        result.append(''.join(word))
        word.clear()


def tokens(value: str) -> list[str]:
    """Follow Node's NODE_OPTIONS double-quote grammar, not POSIX shell quoting."""
    result: list[str] = []
    word: list[str] = []
    quoted = escaped = False
    for char in value:
        if escaped:
            word.append(char)
            escaped = False
            continue
        if char == '\\' and quoted:
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if char == ' ' and not quoted:
            finish_token(result, word)
            continue
        word.append(char)
    if quoted:
        raise ValueError('invalid quoting in Node preload options')
    finish_token(result, word)
    return result


def option_value(words: list[str], index: int, separator: str, inline: str) -> tuple[str, int]:
    """A leading dash in a separate value must use Node's explicit backslash escape."""
    if separator:
        value = inline
    else:
        index += 1
        if index == len(words) or words[index].startswith('-'):
            raise ValueError('missing Node preload option value')
        value = words[index]
        if value.startswith('\\-'):
            value = value[1:]
    if not value:
        raise ValueError('empty Node preload option value')
    return value, index


def options(original: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract literal preload/condition settings while leaving application options intact."""
    setting = original['environment']['NODE_OPTIONS']
    words = tokens(setting.get('value', ''))
    requests = []
    flags = []
    index = 0
    while index < len(words):
        name, separator, inline = words[index].partition('=')
        name = name.replace('_', '-')
        if name in OPTIONS or name in ('--conditions', '-C'):
            position = index
            if separator and name in ('-r', '-C'):
                raise ValueError('Node short preload/condition options require a separate value')
            value, index = option_value(words, index, separator, inline)
            if name in OPTIONS:
                requests.append({'mechanism': OPTIONS[name], 'specifier': value, 'option_index': position})
            else:
                flags.append('--conditions=' + value)
        elif name in ('--preserve-symlinks', '--no-preserve-symlinks'):
            flags.append(name)
        index += 1
    return ([item for item in requests if item['mechanism'] == 'require']
            + [item for item in requests if item['mechanism'] != 'require']), flags


def requested_origin(entry: dict[str, Any], cwd: str) -> dict[str, Any] | None:
    """Keep literal path aliases/absences separate from Node's resolved file target."""
    specifier = entry['specifier']
    if specifier.startswith('file:'):
        filename = Path(unquote(urlsplit(specifier).path))
    elif specifier.startswith('.') or Path(specifier).is_absolute():
        value = specifier if entry['mechanism'] == 'require' else unquote(urlsplit(specifier).path)
        filename = Path(cwd) / value
    else:
        return None
    if not os.path.lexists(filename):
        return {'path': str(filename), 'absent': True}
    if filename.is_dir():
        return {'path': str(filename), 'resolved_path': str(filename.resolve(strict=True)), 'directory': True}
    return identity(filename)


def capture(original: dict[str, Any], environment: dict[str, str]) -> dict[str, Any]:
    """Ask Node to resolve entries without applying application NODE_OPTIONS or code."""
    requests, flags = options(original)
    env = environment.copy()
    for key in ('NODE_PATH', 'NODE_PRESERVE_SYMLINKS'):
        value = original['environment'][key].get('value')
        if value is not None:
            env[key] = value
    command = [original['node'], '--experimental-import-meta-resolve', *flags, str(RESOLVER)]
    result = {'schema_version': 1, 'cwd': original['cwd'], 'requests': requests, 'resolution_flags': flags,
              'resolution_environment': {key: original['environment'][key] for key in ('NODE_PATH', 'NODE_PRESERVE_SYMLINKS')},
              'resolver': None, 'argv': None, 'entries': [], 'coverage': 'resolved_entries_and_nearest_package_context',
              'gaps': []}
    if not requests:
        return result
    result['resolver'] = identity(RESOLVER)
    try:
        resolved = subprocess.run(command, input=json.dumps({'cwd': original['cwd'], 'requests': requests}),
            cwd=original['cwd'], env=env, capture_output=True, text=True, check=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        raise ValueError('Node preload resolution failed') from None
    if identity(RESOLVER) != result['resolver']:
        raise ValueError('Node preload resolver changed during planning')
    result['argv'] = command
    for entry in json.loads(resolved.stdout)['entries']:
        entry['requested_origin'] = requested_origin(entry, original['cwd'])
        if entry['kind'] == 'file':
            filename = Path(unquote(urlsplit(entry['target']).path))
            entry.update(file=identity(filename), package_context=package_context(filename))
        result['entries'].append(entry)
    result['gaps'] = coverage_gaps(result['entries'])
    return result


def coverage_gaps(entries: list[dict[str, Any]]) -> list[str]:
    """Resolved entry bytes do not establish arbitrary loader or remote/inline closure."""
    return sorted({'custom_loader' for item in entries if item['mechanism'] == 'loader'}
                  | {'non_file_preload' for item in entries if item['kind'] == 'url'})


def verify_entry(request: dict[str, Any], entry: dict[str, Any], cwd: str) -> None:
    """Keep each resolved file linked to its request and literal path origin."""
    if (any(entry[key] != request[key] for key in request) or entry['kind'] not in ('file', 'builtin', 'url')
            or entry['requested_origin'] != requested_origin(entry, cwd)):
        raise ValueError('Node preload origin or ordering changed')
    target = urlsplit(entry['target'])
    if entry['kind'] == 'file':
        if target.scheme != 'file' or target.netloc not in ('', 'localhost'):
            raise ValueError('Node preload file has a different URL origin')
        filename = Path(unquote(target.path))
        if identity(filename) != entry['file'] or package_context(filename) != entry['package_context']:
            raise ValueError('Node preload file or package context changed')
        origin = entry['requested_origin']
        if origin and 'sha256' in origin and origin['resolved_path'] != entry['file']['resolved_path']:
            raise ValueError('Node preload target differs from its literal file origin')
    elif entry['kind'] == 'builtin':
        canonical = request['specifier'] if request['specifier'].startswith('node:') else 'node:' + request['specifier']
        if entry['target'] != canonical:
            raise ValueError('Node builtin preload changed')
    elif target.scheme in ('file', 'node', ''):
        raise ValueError('Node preload URL kind changed')


def verify(original: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Recheck retained entry/context identities without executing recorded commands."""
    requests, flags = options(original)
    if (type(metadata['schema_version']) is not int or metadata['schema_version'] != 1 or metadata['cwd'] != original['cwd']
            or metadata['requests'] != requests or metadata['resolution_flags'] != flags
            or metadata['resolution_environment'] != {key: original['environment'][key] for key in ('NODE_PATH', 'NODE_PRESERVE_SYMLINKS')}
            or len(metadata['entries']) != len(requests)
            or metadata['coverage'] != 'resolved_entries_and_nearest_package_context'
            or metadata['gaps'] != coverage_gaps(metadata['entries'])):
        raise ValueError('Node preload request/resolution metadata differs from the request')
    expected_argv = [original['node'], '--experimental-import-meta-resolve', *flags, str(RESOLVER)] if requests else None
    if metadata['argv'] != expected_argv or metadata['resolver'] != (identity(RESOLVER) if requests else None):
        raise ValueError('Node preload tooling differs from the fixed resolver recipe')
    for request, entry in zip(requests, metadata['entries']):
        verify_entry(request, entry, original['cwd'])
