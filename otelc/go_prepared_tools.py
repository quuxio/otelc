"""Reuse fixed installed Go tooling without changing it for application caches."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from ci import build_go_upstream as tooling
from otelc import file_inputs, go_context_bundle as context
from otelc.python_sdk_io import decode

FIELDS = {'schema_version', 'go_version', 'go', 'go_sha256', 'platform', 'module',
          'version', 'module_sum', 'module_go_sum', 'weaver', 'weaver_sha256',
          'bundle', 'bundle_sha256', 'build_metadata'}


def encoded(value: Any) -> str:
    """Compare complete primitive values, distinguishing booleans and integers."""
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))


def identity(path: Path, *, content: bool = False) -> tuple[dict[str, Any], bytes]:
    """Keep bytes and file facts stable across the same bounded regular-file read."""
    before = path.lstat()
    value, raw = file_inputs.checked_file(path, nofollow=True, include_content=content,
        include_stat=True, max_bytes=1024 * 1024 if content else None)
    after = path.lstat()
    facts = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mode,
                           info.st_uid, info.st_gid, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)
    if facts(before) != facts(after):
        raise ValueError('prepared Go tooling changed while reading: ' + path.name)
    value.update(mode=after.st_mode, uid=after.st_uid, gid=after.st_gid,
                 links=after.st_nlink, ctime_ns=after.st_ctime_ns)
    return value, raw


def snapshot(directory: Path, tools: dict[str, Any]) -> dict[str, Any]:
    """Pin the fixed manifest, selected compiler, fixed weaver and bundle facts."""
    paths = {'manifest': directory / 'toolchain.json', 'go': Path(tools['go']),
             'weaver': directory / 'bin/otelc', 'bundle': Path(tools['bundle'])}
    inputs = {name: identity(path)[0] for name, path in paths.items()}
    for name in ('go', 'weaver', 'bundle'):
        if inputs[name]['sha256'] != tools[name + '_sha256']:
            raise ValueError('prepared Go tooling identity changed: ' + name)
    return inputs


@dataclass(frozen=True)
class PreparedTools:
    """Immutable caller-held bindings; edited metadata cannot replace them."""

    directory: Path
    binding: str

    def toolchain(self) -> dict[str, Any]:
        """Return a fresh tool record, retaining no mutable nested aliases."""
        return json.loads(self.binding)['toolchain']

    def verify(self, tools: dict[str, Any]) -> None:
        """Recheck all selected tooling without executing or installing anything."""
        expected = json.loads(self.binding)
        if (encoded(tools) != encoded(expected['toolchain'])
                or snapshot(self.directory, tools) != expected['inputs']):
            raise ValueError('prepared Go tooling changed during application build')


def record(directory: Path, compiler: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Require the installed format and pins before executing either fixed tool."""
    manifest, raw = identity(directory / 'toolchain.json', content=True)
    value = decode(raw)
    pins = {'schema_version': 1, 'go_version': tooling.GO_VERSION,
            'module': context.UPSTREAM_MODULE, 'version': context.UPSTREAM_VERSION,
            'module_sum': context.MODULE_SUM, 'module_go_sum': context.MODULE_GO_SUM,
            'bundle_sha256': context.BUNDLE_SHA256}
    if (set(value) != FIELDS or any(encoded(value[key]) != encoded(expected) for key, expected in pins.items())
            or value['go'] != str(compiler) or value['weaver'] != str(directory / 'bin/otelc')
            or type(value['bundle']) is not str or not Path(value['bundle']).is_absolute()
            or type(value['build_metadata']) is not str):
        raise ValueError('prepared Go tooling requires the pinned manifest and its exact recorded compiler')
    return value, manifest


def query(arguments: list[str], directory: Path, environment: dict[str, str]) -> bytes:
    """Read tool metadata without the installer's error-log or cache writes."""
    result = subprocess.run(arguments, cwd=directory, env=environment, capture_output=True, timeout=60)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise ValueError('prepared Go tooling validation failed: ' + result.stderr.decode(errors='replace')[-4096:])
    return result.stdout


def prepare(go: str, directory: Path) -> PreparedTools:
    """Validate an existing installation; never download, rebuild or rewrite it."""
    selected = str(Path(go).absolute()) if Path(go).is_absolute() else shutil.which(go)
    if not selected:
        raise ValueError('prepared Go compiler is unavailable; select its recorded --go path')
    compiler = Path(selected).resolve(strict=True)
    directory = directory.resolve(strict=True)
    tools, manifest = record(directory, compiler)
    before = snapshot(directory, tools)
    if before['manifest'] != manifest or not all(os.access(path, os.X_OK) for path in
                                                (compiler, directory / 'bin/otelc')):
        raise ValueError('prepared Go tooling changed or is not executable')
    env = tooling.compiler_environment(tools, directory)
    observed = json.loads(query([str(compiler), 'env', '-json', 'GOROOT', 'GOVERSION',
        'GOOS', 'GOARCH', 'GOHOSTOS', 'GOHOSTARCH'], directory, env))
    if (observed.get('GOVERSION') != tooling.GO_VERSION
            or observed.get('GOROOT') != str(compiler.parent.parent)
            or observed.get('GOOS') != observed.get('GOHOSTOS')
            or observed.get('GOARCH') != observed.get('GOHOSTARCH')
            or tools['platform'] != {key: observed.get(key) for key in ('GOOS', 'GOARCH')}):
        raise ValueError('prepared Go tooling requires the recorded native Go1.27.3')
    weaver = directory / 'bin/otelc'
    metadata = query([str(compiler), 'version', '-m', str(weaver)], directory, env).decode()
    module = '\tmod\t' + context.UPSTREAM_MODULE + '\t' + context.UPSTREAM_VERSION + '\t' + context.MODULE_SUM
    if (metadata != tools['build_metadata'] or module not in metadata or '\tpath\t' + tooling.PACKAGE not in metadata
            or query([str(weaver), 'version'], directory, env).decode().strip() != 'otelc version ' + context.UPSTREAM_VERSION):
        raise ValueError('prepared Go instrumentation executable metadata differs')
    binding = PreparedTools(directory, encoded({'toolchain': tools, 'inputs': before}))
    binding.verify(tools)
    return binding
