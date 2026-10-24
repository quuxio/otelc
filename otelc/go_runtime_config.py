"""Translate the common policy into a private, digest-bound owned Go SDK plan."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from otelc.config import Policy
from otelc.file_inputs import checked_file

MAX_CONFIG_BYTES = 4 * 1024 * 1024


def configuration(policy: Policy, functions: tuple[str, ...], lifetimes: tuple[tuple[str, bool], ...],
                  selection_sha256: str) -> dict[str, Any]:
    """Consume resolved selections; resolving original source is a separate step."""
    data = policy.data
    if 'go' not in data['languages']:
        raise ValueError('Go is not enabled by the common policy')
    if not isinstance(selection_sha256, str) or re.fullmatch('[0-9a-f]{64}', selection_sha256) is None:
        raise ValueError('invalid Go selection identity')
    if (type(functions) is not tuple or any(type(name) is not str for name in functions)
            or len(functions) > 4096 or len(set(functions)) != len(functions)
            or any(type(name) is not str or not name or len(name.encode()) > 4096
                   or not policy.selection('functions').accepts(name) for name in functions)):
        raise ValueError('invalid or unselected resolved Go function')
    if (type(lifetimes) is not tuple or any(type(item) is not tuple or len(item) != 2
            or type(item[0]) is not str or type(item[1]) is not bool for item in lifetimes)
            or len(lifetimes) > 4096 or len({name for name, _ in lifetimes}) != len(lifetimes)
            or lifetimes and not data['lifetimes']['enabled']
            or any(type(name) is not str or not name or len(name.encode()) > 4096
                   or type(resource) is not bool or not policy.selection('lifetimes').accepts(name)
                   for name, resource in lifetimes)):
        raise ValueError('invalid or unselected resolved Go lifetime')
    return {'schema_version': 1, 'service_name': data['resource']['service_name'],
            'endpoint': data['export']['endpoint'], 'interval_ms': data['export']['interval_ms'],
            'timeout_ms': data['export']['timeout_ms'], 'shutdown_timeout_ms': data['export']['shutdown_timeout_ms'],
            'collection': data['metrics']['collection'], 'emission': data['metrics']['emission'],
            'traces_enabled': data['traces']['enabled'], 'sample_ratio': data['traces']['sample_ratio'],
            'functions': list(functions),
            'lifetimes': [{'name': name, 'resource': resource} for name, resource in lifetimes],
            'max_live': data['lifetimes']['max_live'],
            'boundaries': list(data['metrics']['histogram_boundaries_seconds']),
            'policy_sha256': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
            'selection_sha256': selection_sha256}


def prepare(policy: Policy, directory: Path, *, functions: tuple[str, ...],
            lifetimes: tuple[tuple[str, bool], ...], selection_sha256: str) -> dict[str, Any]:
    """Publish one new private configuration, preserving all existing paths."""
    value = configuration(policy, functions, lifetimes, selection_sha256)
    if any(name.startswith('OTEL_') for name in os.environ):
        raise ValueError('Go profile requires unset ambient OTEL_* SDK variables')
    encoded = (json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':')) + '\n').encode()
    if len(encoded) > MAX_CONFIG_BYTES:
        raise ValueError('resolved Go runtime configuration exceeds its byte bound')
    directory = directory.absolute()
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    path = directory / 'config.json'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb', closefd=False) as stream:
            stream.write(encoded)
    finally:
        os.close(descriptor)
    identity, _ = checked_file(path, nofollow=True, max_bytes=MAX_CONFIG_BYTES)
    return {'schema_version': 1, 'kind': 'otelc.go-runtime-config', 'directory': str(directory),
            'config': identity, 'health_version': 3,
            'environment': {'OTELC_GO_RUNTIME': str(directory), 'OTELC_GO_CONFIG_SHA256': identity['sha256']}}
