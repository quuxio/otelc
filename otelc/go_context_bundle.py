"""Prepare the pinned official Go context hooks without changing application input."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_MODULE = 'go.opentelemetry.io/otelc'
UPSTREAM_VERSION = 'v1.0.1'
UPSTREAM_COMMIT = '3b9beed7a1200cf16e59bafa431dd4c4a0601f41'
MODULE_SUM = 'h1:HSgkn7kGhHe4kjtv+ex0ZFRbC66sc+c5KcEHgDXuwxE='
MODULE_GO_SUM = 'h1:fOCi1cByxU0s3sOwXPp5/gAnxsPl5KsTM9elgtBIESE='
BUNDLE_SHA256 = '4a928c69ce269e37c98413c93159713fa7bcd8169c5d6b4de167305dc5ca9bb2'
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MANIFEST = 'context-bundle.json'
TRACE_PREFIX = 'instrumentation_temp/go.opentelemetry.io/otel/sdk/trace/'
TRACE_OUTPUT = 'instrumentation/go.opentelemetry.io/otel/sdk/trace/'

# Only the context bridge is used from pkg/runtime. Its independent SDK setup,
# signal handling and logging do not belong in the application's owned pipeline.
INPUTS = {
    **{TRACE_PREFIX + name: TRACE_OUTPUT + name for name in (
        'gls.go', 'go.mod', 'go.sum', 'hook.go', 'otel_trace_context.go', 'otelc.yaml')},
    **{'instrumentation_temp/runtime/' + name: 'instrumentation/runtime/' + name
       for name in ('go.mod', 'otelc.yaml', 'runtime_gls.go', 'stub.go')},
    'pkg_temp/go.mod': 'pkg/go.mod',
    'pkg_temp/hook/context.go': 'pkg/hook/context.go',
    'pkg_temp/runtime/trace_context.go': 'pkg/runtime/trace_context.go',
}
BRIDGE_MODULE = b'''// Generated context bridge subset; see NOTICE.upstream.
module go.opentelemetry.io/otelc/pkg/runtime

go 1.26.0

require go.opentelemetry.io/otel/trace v1.47.0
'''


def digest(value: bytes) -> str:
    """Return the content identity retained in the generated-source manifest."""
    return hashlib.sha256(value).hexdigest()


def _members(data: bytes) -> dict[str, bytes]:
    result = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for member in archive.getmembers():
            if member.name not in INPUTS:
                continue
            if member.name in result or not member.isfile():
                raise ValueError('ambiguous Go upstream context member')
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError('missing Go upstream context member')
            with stream:
                result[member.name] = stream.read()
    if result.keys() != INPUTS.keys():
        raise ValueError('incomplete Go upstream context bundle')
    return result


def _generated(original: dict[str, bytes], tasks: bool) -> dict[str, bytes]:
    result = {INPUTS[name]: data for name, data in original.items()}
    if tasks:
        path = TRACE_OUTPUT + 'otel_trace_context.go'
        old = b'sw := &spanWrapper{last, nil, tc.sw.ended}'
        new = (b'inherited := trace.SpanFromContext(trace.ContextWithSpanContext(context.Background(), last.SpanContext()))\n'
               b'\tsw := &spanWrapper{inherited, nil, &atomic.Bool{}}')
        source = result[path]
        if source.count(old) != 1 or source.count(b'import (\n') != 1:
            raise ValueError('Go upstream context Clone does not match the pinned patch')
        result[path] = source.replace(b'import (\n', b'import (\n\t"context"\n', 1).replace(old, new)
    else:
        path = 'instrumentation/runtime/otelc.yaml'
        marker = b'goroutine_propagate:\n'
        if result[path].count(marker) != 1:
            raise ValueError('Go upstream task rule does not match the pinned profile')
        # g structures are reused. Disabling inheritance must still clear both
        # added fields, or a reused goroutine can retain unrelated old context.
        result[path] = result[path].split(marker)[0] + b'''goroutine_context_reset:
  target: "runtime"
  where:
    func: "newproc1"
  do:
    - inject_code:
        raw: |
          defer func(){
            _unnamedRetVal0.otel_trace_context = nil;
            _unnamedRetVal0.otel_baggage_container = nil;
          }()
'''
    result['pkg/runtime/go.mod'] = BRIDGE_MODULE
    result['LICENSE.upstream'] = (ROOT / 'licenses/Apache-2.0-opentelemetry.txt').read_bytes()
    result['NOTICE.upstream'] = (
        'Selected context source from the OpenTelemetry Authors, Apache-2.0.\n'
        f'{UPSTREAM_MODULE}@{UPSTREAM_VERSION}, commit {UPSTREAM_COMMIT}.\n'
        'SDK/context storage and hooks are reused; SDK initialisation is excluded.\n'
        'The generated Clone captures immutable inherited SpanContext when tasks are enabled.\n'
        'The goroutine hook resets context without inheritance when tasks are disabled.\n'
    ).encode('ascii')
    return result


def _manifest(original: dict[str, bytes], generated: dict[str, bytes], tasks: bool) -> dict[str, Any]:
    return {
        'schema_version': 1, 'upstream_module': UPSTREAM_MODULE,
        'upstream_version': UPSTREAM_VERSION, 'upstream_commit': UPSTREAM_COMMIT,
        'module_sum': MODULE_SUM, 'module_go_sum': MODULE_GO_SUM,
        'bundle_sha256': BUNDLE_SHA256, 'tasks': tasks,
        'input_files': {name: digest(data) for name, data in original.items()},
        'files': {name: digest(data) for name, data in generated.items()},
        'scope': 'generated context hooks only; not Go adapter or provider admission',
    }


def _manifest_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def prepare(bundle: Path, output: Path, *, tasks: bool) -> dict[str, Any]:
    """Write a fresh, fixed source set for the selected common-policy task switch."""
    if type(tasks) is not bool:
        raise ValueError('Go task context switch must be boolean')
    with bundle.open('rb') as stream:
        data = stream.read(MAX_BUNDLE_BYTES + 1)
    if len(data) > MAX_BUNDLE_BYTES or digest(data) != BUNDLE_SHA256:
        raise ValueError('Go upstream context bundle does not match the pinned release')
    original = _members(data)
    generated = _generated(original, tasks)
    output.mkdir(mode=0o700)
    for name, content in generated.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(content)
        path.chmod(0o600)
    value = _manifest(original, generated, tasks)
    manifest = output / MANIFEST
    with manifest.open('xb') as stream:
        stream.write(_manifest_bytes(value))
    manifest.chmod(0o600)
    return value


def verify(bundle: Path, output: Path, *, tasks: bool) -> dict[str, Any]:
    """Reproduce expected bytes rather than trusting a self-consistent file manifest."""
    if type(tasks) is not bool:
        raise ValueError('Go task context switch must be boolean')
    with bundle.open('rb') as stream:
        data = stream.read(MAX_BUNDLE_BYTES + 1)
    if len(data) > MAX_BUNDLE_BYTES or digest(data) != BUNDLE_SHA256:
        raise ValueError('Go upstream context bundle does not match the pinned release')
    original = _members(data)
    generated = _generated(original, tasks)
    expected = {name: digest(data) for name, data in generated.items()}
    value = _manifest(original, generated, tasks)
    manifest = output / MANIFEST
    if manifest.is_symlink() or manifest.read_bytes() != _manifest_bytes(value):
        raise ValueError('Go upstream context manifest differs from the fixed profile')
    actual = {str(path.relative_to(output)) for path in output.rglob('*') if path.is_file()}
    if actual != set(expected) | {MANIFEST}:
        raise ValueError('Go upstream context source set changed')
    if any((output / name).is_symlink() or digest((output / name).read_bytes()) != checksum
           for name, checksum in expected.items()):
        raise ValueError('Go upstream context source bytes changed')
    return value


def replacements(output: Path) -> dict[str, Path]:
    """Return fixed local module roots for the private application's build recipe."""
    base = output.resolve()
    return {
        'go.opentelemetry.io/otelc/pkg': base / 'pkg',
        'go.opentelemetry.io/otelc/pkg/runtime': base / 'pkg/runtime',
        'go.opentelemetry.io/otelc/instrumentation/runtime': base / 'instrumentation/runtime',
        'go.opentelemetry.io/otelc/instrumentation/go.opentelemetry.io/otel/sdk/trace': base / TRACE_OUTPUT,
    }
