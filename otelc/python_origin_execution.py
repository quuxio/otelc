"""Present bounded journal-2 capture without claiming whole-program provenance."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from otelc.file_inputs import identity
from otelc import python_origin_journal as origins
from otelc.python_input_journal import SourceInput


def read(path: Path, *, expected_pid: int, entry: dict[str, Any],
         root: Path, plain: bool) -> dict[str, Any]:
    """Bind consumed entry and selected SDK joins to independently prepared intent."""
    value = origins.read(path, expected_pid=expected_pid)
    failures = []
    if not value.capture_complete:
        failures.append('native_capture')
    entry_roots = [item for item in value.roots if item.route == 1]
    source = value.sources[0][1] if len(value.sources) == 1 else None
    entry_sequence = value.sources[0][0] if source is not None else None
    if (not value.entry_expected or source is None or source.sha256 != entry['sha256']
            or _resolved(source.path, root) != Path(entry['resolved_path'])
            or len(entry_roots) != 1 or entry_roots[0].input_sequence != entry_sequence):
        failures.append('entry_origin')
    if any(not _resolved(item.descriptor.path, root).is_relative_to(root) for item in value.roots):
        failures.append('root_scope')
    codes = {item.identity for item in value.codes}
    joined = {sdk for sdk, _ in value.sdk_joins}
    if plain and (codes or joined):
        failures.append('baseline_sdk_observations')
    elif not plain and codes != joined:
        failures.append('selected_sdk_origins')
    return {'phase': 'failed' if failures else 'complete',
            'python_finalized': value.python_finalized,
            'capture_failures': value.capture_failures, 'journal_failures': value.journal_failures,
            'journal': identity(path),
            'sources': [_input(item) for _, item in value.sources],
            'imports': [_input(item) for _, item in value.imports],
            'codes': [{'id': item.identity, 'path_hex': item.path.hex(), 'sha256': item.sha256,
                       'offset': item.offset, 'python_version': item.python_version,
                       'descriptor_version': item.descriptor_version} for item in value.codes],
            'origins': {'schema_version': 2, 'scope_root': str(root),
                        'root_ids': [item.identity for item in value.roots],
                        'execution_ids': [item.identity for item in value.executions],
                        'sdk_joins': [list(item) for item in value.sdk_joins], 'failures': failures}}


def _resolved(path: bytes, root: Path) -> Path:
    selected = Path(os.fsdecode(path))
    return (selected if selected.is_absolute() else root / selected).resolve(strict=True)


def _input(item: SourceInput) -> dict[str, Any]:
    return {'path_hex': item.path.hex(), 'sha256': item.sha256,
            'device': item.device, 'inode': item.inode, 'mtime_ns': item.mtime_ns}
