"""Read original module locations for a separate private-caller compiler profile."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from otelc.file_inputs import checked_file
from otelc.python_sdk_io import decode

LIMIT = 8 * 1024 * 1024


def read(path: Path, root: Path, entry: Path, sources: dict[str, str], *,
         crate_name: str) -> dict[str, Any]:
    """Check a closed module inventory against caller-held original source bytes.

    This reader validates a retained compiler statement. It does not establish
    that a compiler ran, execute callers or prove complete dependency closure.
    """
    if (root != root.resolve(strict=True) or not root.is_dir()
            or type(sources) is not dict or not 1 <= len(sources) <= 65536
            or str(entry) not in sources or type(crate_name) is not str or not crate_name
            or len(crate_name.encode()) > 4096 or '\0' in crate_name):
        raise ValueError('Rust module inventory requires bounded original inputs')
    sizes = {}
    for name, expected in sources.items():
        if type(name) is not str or type(expected) is not str:
            raise ValueError('invalid Rust module source binding')
        source = Path(name)
        if (not source.is_absolute() or source.resolve(strict=True) != source
                or not source.is_relative_to(root)):
            raise ValueError('Rust module sources must be original physical inputs')
        _, raw = checked_file(source, nofollow=True, include_content=True, max_bytes=LIMIT)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Rust module source bytes changed')
        sizes[name] = len(raw)
    _, raw = checked_file(path, nofollow=True, include_content=True, max_bytes=LIMIT)
    value = decode(raw)
    fields = {'schema_version', 'kind', 'mode', 'crate_name', 'byte_coordinates', 'sources', 'modules', 'source_complete'}
    if (type(value) is not dict or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['kind'] != 'otelc.rust.module-inventory'
            or value['crate_name'] != crate_name
            or value['mode'] != 'module-inspect' or value['sources'] != sources
            or value['byte_coordinates'] != 'original-utf8'
            or value['source_complete'] is not False
            or type(value['modules']) is not list or not 1 <= len(value['modules']) <= 65536):
        raise ValueError('Rust module inventory differs from requested inputs')
    rows = value['modules']
    names = []
    row_fields = {'compiler_name', 'parent', 'kind', 'declaration', 'body', 'eligible', 'reason'}
    for row in rows:
        if (type(row) is not dict or set(row) != row_fields
                or type(row['compiler_name']) is not str
                or len(row['compiler_name'].encode()) > 4096 or '\0' in row['compiler_name']
                or type(row['kind']) is not str or row['kind'] not in ('crate', 'inline', 'external')
                or type(row['eligible']) is not bool
                or row['reason'] not in (None, 'expanded', 'block_module')
                or row['eligible'] != (row['reason'] is None)):
            raise ValueError('invalid Rust module record')
        for field in ('declaration', 'body'):
            location = row[field]
            if (type(location) is not dict or set(location) != {'file', 'start', 'end', 'expanded'}
                    or type(location['file']) is not str or location['file'] not in sizes
                    or type(location['expanded']) is not bool
                    or type(location['start']) is not int or type(location['end']) is not int
                    or not 0 <= location['start'] <= location['end'] <= sizes[location['file']]):
                raise ValueError('invalid Rust module original byte interval')
        expanded = row['declaration']['expanded'] or row['body']['expanded']
        if expanded and row['reason'] != 'expanded':
            raise ValueError('expanded Rust module cannot gain a physical caller route')
        if row['kind'] == 'crate':
            if (row['compiler_name'] != '' or row['parent'] is not None or row['declaration'] != row['body']
                    or row['body']['file'] != str(entry) or not row['eligible']):
                raise ValueError('Rust module crate root differs from original entry')
        elif (not row['compiler_name'] or type(row['parent']) is not str
                or row['parent'] != row['compiler_name'].rpartition('::')[0]
                or (row['kind'] == 'inline') != (row['declaration']['file'] == row['body']['file'])):
            raise ValueError('Rust module declaration/body relationship differs')
        names.append(row['compiler_name'])
    if names != sorted(set(names)) or sum(row['kind'] == 'crate' for row in rows) != 1:
        raise ValueError('Rust module identities must be ordered, unique and rooted')
    by_name = {row['compiler_name']: row for row in rows}
    for row in rows:
        parent = by_name.get(row['parent'])
        if row['kind'] == 'crate':
            continue
        declaration, body = row['declaration'], row['body']
        if parent is not None and not declaration['expanded']:
            owner = parent['body']
            if (declaration['file'] != owner['file']
                    or not owner['start'] <= declaration['start'] <= declaration['end'] <= owner['end']):
                raise ValueError('Rust module declaration is outside its original parent body')
        if row['kind'] == 'inline' and not declaration['expanded'] and not body['expanded']:
            if not declaration['start'] <= body['start'] <= body['end'] <= declaration['end']:
                raise ValueError('Rust inline module body is outside its original declaration')
        if row['eligible'] and (parent is None or not parent['eligible']):
            raise ValueError('Rust caller route lacks an eligible original module ancestor')
        if row['reason'] == 'block_module' and parent is not None and parent['reason'] != 'block_module':
            raise ValueError('Rust block-module reason contradicts its parent route')
        if (row['reason'] == 'expanded' and not row['declaration']['expanded']
                and not row['body']['expanded'] and (parent is None or parent['reason'] != 'expanded')):
            raise ValueError('Rust expanded-module reason has no expanded route')
    return value
