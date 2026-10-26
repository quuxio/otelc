"""Bind an existing standalone Rust build to the fixed pre-exec ownership gate."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any

from otelc import native_build, native_gate, rust_build, rust_compiler
from otelc.config import Policy
from otelc.file_inputs import checked_file
from otelc.python_sdk_io import decode

MANIFEST = 'rust-owned-build.json'
PROFILE = 'rust1981-owned-standalone-v1'
FIELDS = {'schema_version', 'kind', 'profile', 'source_profile', 'source_build', 'gate', 'source_complete'}
GATE_FIELDS = {'schema_version', 'kind', 'source', 'compiler', 'executable', 'coverage', 'command', 'source_complete'}


def same(value: Any, expected: Any) -> bool:
    """Keep boolean, integer and float identities distinct throughout records."""
    return json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)


def envelope(raw: bytes) -> dict[str, Any]:
    """Refuse unknown profiles before following the declared build reference."""
    document = decode(raw)
    if (type(document) is not dict or set(document) != FIELDS
            or type(document['schema_version']) is not int or document['schema_version'] != 1
            or document['kind'] != 'otelc.rust.owned-build' or document['profile'] != PROFILE
            or document['source_profile'] not in (rust_build.PROFILE, rust_build.LIFETIME_PROFILE)
            or document['source_complete'] is not False
            or type(document['source_build']) is not dict
            or type(document['source_build'].get('path')) is not str):
        raise ValueError('Rust owned envelope needs its fixed profile and original build binding')
    return document


def outside(output: Path, source: rust_build.Build, metadata: dict[str, Any]) -> None:
    """Refuse physical source, build, compiler and SDK overlaps before writes."""
    tools = rust_compiler.read(Path(metadata['tools']['path']).parent).manifest()
    sdk = native_build.sdk(Path(metadata['sdk']['path']).parent)
    roots = (Path(metadata['root']), source.directory,
             Path(metadata['tools']['path']).parent, Path(metadata['sdk']['path']).parent,
             native_gate.SOURCE.parent, rust_compiler.ROOT / 'native/rust',
             Path(__file__).resolve().parent, Path(tools['sysroot']),
             Path(sdk['toolchain']['directory']).resolve(strict=True).parent,
             *(Path(row['path']).parent for row in (*tools['sources'], *tools['tools'],
                                                   *sdk['sources'], *sdk['build_tools'])))
    if any(rust_compiler.physical_within(output, root) for root in roots):
        raise ValueError('Rust owned output must be outside original source/build/tool inputs')


@dataclass(frozen=True)
class Build:
    """Hold original build and gate facts independently of mutable returned records."""

    directory: Path
    encoded: str
    source: rust_build.Build
    gate: native_gate.Product

    def manifest(self) -> dict[str, Any]:
        """Return a fresh decoded envelope without mutable aliases."""
        return decode(self.encoded.encode())


def verify(value: Build, policy: Policy) -> dict[str, Any]:
    """Recheck the held original build and fixed gate without compiling anything."""
    if type(value) is not Build:
        raise ValueError('Rust owned verification needs a caller-held build')
    _, raw = checked_file(value.directory / MANIFEST, nofollow=True, include_content=True,
                          max_bytes=64 * 1024)
    if raw.decode() != value.encoded:
        raise ValueError('Rust owned build record changed')
    metadata = rust_build.verify(value.source, policy)
    outside(value.directory, value.source, metadata)
    expected = {'schema_version': 1, 'kind': 'otelc.rust.owned-build', 'profile': PROFILE,
                'source_profile': metadata['profile'],
                'source_build': native_gate.identity(value.source.directory / rust_build.MANIFEST),
                'gate': native_gate.identity(value.directory / 'gate/gate.json'), 'source_complete': False}
    document = envelope(raw)
    if not same(document, expected):
        raise ValueError('Rust owned build profile or original binding changed')
    _, gate_raw = checked_file(value.directory / 'gate/gate.json', nofollow=True,
                               include_content=True, max_bytes=64 * 1024)
    if gate_raw.decode() != value.gate.binding:
        raise ValueError('Rust owned gate record changed')
    gate = value.gate.metadata()
    if (type(gate) is not dict or set(gate) != GATE_FIELDS
            or type(gate['schema_version']) is not int or gate['schema_version'] != 1
            or gate['kind'] != 'otelc.native.exec-gate' or gate['coverage'] is not False
            or gate['source_complete'] is not False
            or not same(gate['source'], native_gate.identity(native_gate.SOURCE))
            or not same(gate['executable'], native_gate.identity(value.directory / 'gate/otelc-exec-gate'))
            or type(gate['compiler']) is not dict or type(gate['compiler'].get('path')) is not str):
        raise ValueError('Rust owned build requires the fixed version-one exec gate')
    if not same(gate['compiler'], native_gate.identity(Path(gate['compiler']['path']))):
        raise ValueError('Rust owned gate compiler identity changed')
    command = [gate['compiler']['path'], '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
               str(native_gate.SOURCE), '-o', str(value.directory / 'gate/otelc-exec-gate')]
    if gate['command'] != command:
        raise ValueError('Rust owned gate command is not its fixed recipe')
    value.gate.verify()
    return document


def build(policy: Policy, source: rust_build.Build, output: Path) -> Build:
    """Compile only the fixed gate around a previously verified external build."""
    metadata = rust_build.verify(source, policy)
    outside(output, source, metadata)
    if shutil.disk_usage(output.parent).free < 8 * 1024**3:
        raise ValueError('Rust owned gate compilation requires at least 8 GiB free')
    original = native_gate.identity(source.directory / rust_build.MANIFEST)
    output.mkdir(mode=0o700)
    output = output.resolve(strict=True)
    outside(output, source, metadata)
    compiled_gate = native_gate.build(output / 'gate')
    _, gate_raw = checked_file(output / 'gate/gate.json', nofollow=True,
                               include_content=True, max_bytes=64 * 1024)
    if not same(decode(gate_raw), compiled_gate.metadata()):
        raise ValueError('Rust owned gate publication differs from its compiled product')
    gate = native_gate.Product(gate_raw.decode())
    if (rust_build.verify(source, policy) != metadata
            or native_gate.identity(source.directory / rust_build.MANIFEST) != original):
        raise ValueError('Rust original build changed during owned gate compilation')
    document = {'schema_version': 1, 'kind': 'otelc.rust.owned-build', 'profile': PROFILE,
                'source_profile': metadata['profile'], 'source_build': original,
                'gate': native_gate.identity(output / 'gate/gate.json'), 'source_complete': False}
    encoded = json.dumps(document, sort_keys=True, indent=2) + '\n'
    with (output / MANIFEST).open('x') as stream:
        stream.write(encoded)
    result = Build(output, encoded, source, gate)
    verify(result, policy)
    return result


def read(directory: Path, policy: Policy, *, source: rust_build.Build | None = None) -> Build:
    """Load the operator-selected envelope, optionally requiring a held source build."""
    directory = directory.resolve(strict=True)
    _, raw = checked_file(directory / MANIFEST, nofollow=True, include_content=True, max_bytes=64 * 1024)
    document = envelope(raw)
    path = Path(document['source_build']['path'])
    if (not path.is_absolute() or path.name != rust_build.MANIFEST
            or path.resolve(strict=True) != path or not same(native_gate.identity(path), document['source_build'])):
        raise ValueError('Rust owned original source-build identity changed')
    if source is None:
        source = rust_build.read(path.parent, policy)
    elif type(source) is not rust_build.Build or source.directory / rust_build.MANIFEST != path:
        raise ValueError('Rust owned envelope does not identify the caller-held source build')
    _, gate_raw = checked_file(directory / 'gate/gate.json', nofollow=True,
                               include_content=True, max_bytes=64 * 1024)
    result = Build(directory, raw.decode(), source, native_gate.Product(gate_raw.decode()))
    verify(result, policy)
    return result
