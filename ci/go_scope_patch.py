"""Apply one reviewed backport to a private copy of the pinned Go weaver."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import stat
from typing import Any

from otelc import go_context_bundle as context
from otelc.python_sdk_io import decode

PROFILE = 'raw-empty-globals-v1'
VERSION = context.UPSTREAM_VERSION + '+quux.scope.1'
FILE = 'tool/internal/instrument/apply_func.go'
BEFORE = '16810f7c3718137c906e76a9e364d2e346172f959e0f8873c3180a3a7898e820'
AFTER = 'd7f7e85feb4608e2c8168753f01b822e16491b43fb72420074129f183adf1422'
ANCHOR = b'func (ip *InstrumentPhase) writeGlobals(pkgName string) error {\n'
GUARD = (b'\t// Raw-only packages have no trampolines and need no HookContext API.\n'
         b'\tif len(ip.varDecls) == 0 {\n\t\treturn nil\n\t}\n')
MANIFEST = 'scope-source.json'


def digest(raw: bytes) -> str:
    """Identify retained source bytes without using mutable build metadata."""
    return hashlib.sha256(raw).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    """Read only regular source files; never follow links out of the copy."""
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError('Go backport source root must be a real directory')
    value = {}
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError('Go backport source must contain only regular files')
        value[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return value


def patched(raw: bytes) -> bytes:
    """Refuse drift rather than applying a fuzzy patch to another release."""
    if digest(raw) != BEFORE or raw.count(ANCHOR) != 1:
        raise ValueError('Go backport requires the exact pinned upstream source')
    result = raw.replace(ANCHOR, ANCHOR + GUARD, 1)
    if digest(result) != AFTER:
        raise ValueError('Go backport differs from the reviewed result')
    return result


def prepare(module: Path, output: Path) -> dict[str, Any]:
    """Copy verified module sources, patch one file, and retain both identities."""
    before = inventory(module)
    raw = (module / FILE).read_bytes()
    replacement = patched(raw)
    source = output / 'source'
    # A new destination keeps a failed or previous installation reviewable.
    source.mkdir()
    shutil.copytree(module, source, dirs_exist_ok=True)
    target = source / FILE
    target.chmod(0o600)
    target.write_bytes(replacement)
    after = before | {FILE: AFTER}
    if inventory(module) != before or inventory(source) != after:
        raise ValueError('Go backport source changed while making its private copy')
    value = {'schema_version': 1, 'kind': 'otelc.go-weaver-source',
             'profile': PROFILE, 'base_module': context.UPSTREAM_MODULE,
             'base_version': context.UPSTREAM_VERSION, 'module_sum': context.MODULE_SUM,
             'module_go_sum': context.MODULE_GO_SUM, 'version': VERSION,
             'patch': {'file': FILE, 'before_sha256': BEFORE, 'after_sha256': AFTER},
             'base_files': before, 'files': after}
    (output / MANIFEST).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    return value


def verify(output: Path, expected_sha256: str) -> None:
    """Recheck fixed retained sources and the single declared patch, read-only."""
    raw = (output / MANIFEST).read_bytes()
    if digest(raw) != expected_sha256:
        raise ValueError('Go backport source manifest changed')
    value = decode(raw)
    pins = {'schema_version': 1, 'kind': 'otelc.go-weaver-source',
            'profile': PROFILE, 'base_module': context.UPSTREAM_MODULE,
            'base_version': context.UPSTREAM_VERSION, 'module_sum': context.MODULE_SUM,
            'module_go_sum': context.MODULE_GO_SUM, 'version': VERSION,
            'patch': {'file': FILE, 'before_sha256': BEFORE, 'after_sha256': AFTER}}
    if (set(value) != set(pins) | {'base_files', 'files'}
            or any(type(value[key]) is not type(pin) or value[key] != pin
                   for key, pin in pins.items())
            or not isinstance(value['base_files'], dict)
            or value['base_files'].get(FILE) != BEFORE
            or value['files'] != value['base_files'] | {FILE: AFTER}
            or inventory(output / 'source') != value['files']):
        raise ValueError('Go backport retained source differs from its fixed profile')
