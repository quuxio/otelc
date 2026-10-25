"""Reproduce a pinned official HTTP module with formatter-preserving telemetry."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any
import zipfile

from ci import build_go_upstream as tooling

MODULE = 'go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp'
VERSION = 'v0.72.0'
SUM = 'h1:LxwW/9ctSCv+QkE/cLR7M91ZIkXNMqJtEMi1vCw9U8s='
MOD_SUM = 'h1:tOsftB4SslBwwErVEPaenU2RpThXWPIU8DoJHEC4dyw='
ZIP_SHA256 = 'e9e9f34bc563d5114bc6c2760cf2b3d15ae82b0bbd635cd534ae3ccce2ac3300'
MAX_BYTES = 16 * 1024 * 1024


def sources(archive: Path) -> dict[str, bytes]:
    """Check actual archive bytes before reading fixed production members."""
    if archive.stat().st_size > MAX_BYTES or hashlib.sha256(archive.read_bytes()).hexdigest() != ZIP_SHA256:
        raise ValueError('Go official HTTP archive differs from the pinned identity')
    prefix = MODULE + '@' + VERSION + '/'
    result: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(archive) as zipped:
        for row in zipped.infolist():
            if not row.filename.startswith(prefix):
                raise ValueError('Go HTTP archive has a different module root')
            name = row.filename[len(prefix):]
            if (Path(name).is_absolute() or '..' in Path(name).parts or name in result
                    or row.file_size > MAX_BYTES):
                raise ValueError('invalid Go HTTP archive member')
            if not (name in ('LICENSE', 'go.mod', 'go.sum')
                    or name.endswith('.go') and not name.endswith('_test.go')):
                continue
            total += row.file_size
            if total > MAX_BYTES:
                raise ValueError('Go HTTP production source exceeds its byte bound')
            result[name] = zipped.read(row)
    if not {'LICENSE', 'go.mod', 'go.sum', 'handler.go', 'transport.go'} <= result.keys():
        raise ValueError('Go HTTP production source is incomplete')
    # OTel error.type remains the official reflection-based concrete type. An
    # error description must not execute an application's Error() implementation.
    old = b'SetStatus(codes.Error, err.Error())'
    if result['transport.go'].count(old) != 3:
        raise ValueError('Go HTTP formatter patch differs from the pinned source')
    result['transport.go'] = result['transport.go'].replace(old, b'SetStatus(codes.Error, "")')
    return result


def prepare(tools: dict[str, Any], output: Path) -> dict[str, Any]:
    """Materialise only the official module's required runtime source and licence."""
    environment = tooling.compiler_environment(tools, Path(__file__).resolve().parents[1] / 'build/go-upstream')
    command = [tools['go'], 'mod', 'download', '-json', MODULE + '@' + VERSION]
    result = subprocess.run(command, env=environment, capture_output=True, check=True, timeout=180)
    identity = json.loads(result.stdout)
    if (identity.get('Path') != MODULE or identity.get('Version') != VERSION
            or identity.get('Sum') != SUM or identity.get('GoModSum') != MOD_SUM):
        raise ValueError('Go official HTTP module differs from the pinned checksums')
    generated = sources(Path(identity['Zip']))
    output.mkdir(mode=0o700)
    for name, data in generated.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)
    manifest = {'schema_version': 1, 'module': MODULE, 'version': VERSION, 'sum': SUM,
        'go_mod_sum': MOD_SUM, 'zip_sha256': ZIP_SHA256,
        'patch': 'three telemetry error descriptions empty; original error objects unchanged',
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in generated.items()}}
    (output / 'otelc-http-module.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest
