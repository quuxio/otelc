"""Read compiler-owned Rust lifetime discovery and apply the common selectors.

This validates metadata and original inputs, not compiler execution provenance
or automatic instrumentation. All outputs retain incomplete coverage explicitly.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from otelc.config import Policy, Selection
from otelc.file_inputs import checked_file
from otelc.python_sdk_io import decode

SOURCE_FIELDS = {'identity', 'file', 'compiler_name', 'start', 'end', 'asynchronous', 'expanded'}
VALUE_FIELDS = {'local', 'role', 'source', 'compiler_type', 'rendered_type', 'kind',
                'needs_drop', 'size', 'alignment', 'layout_error'}
UNAVAILABLE_FIELDS = {'local', 'role', 'compiler_type', 'rendered_type', 'kind', 'needs_drop'}
SITE_FIELDS = {'kind', 'block', 'statement', 'local', 'other', 'projected', 'cleanup', 'unwind'}
SITE_KINDS = {'construction', 'assignment', 'move', 'copy', 'aggregate_input',
              'drop', 'call_return', 'call_argument'}


def closed(value: Any, fields: set[str], label: str) -> dict:
    """Reject extra/missing fields before indexing a metadata object."""
    if type(value) is not dict or set(value) != fields:
        raise ValueError('invalid Rust lifetime ' + label)
    return value


def number(value: Any, limit: int, label: str) -> int:
    """Keep wire booleans and floating-point values out of integer positions."""
    if type(value) is not int or not 0 <= value < limit:
        raise ValueError('invalid Rust lifetime ' + label)
    return value


def source(value: Any, root: Path, inputs: dict[str, bytes]) -> str:
    """Bind declaration identity and byte range to one original source input."""
    row = closed(value, SOURCE_FIELDS, 'source')
    if (any(type(row[key]) is not str or not row[key] for key in ('identity', 'file', 'compiler_name'))
            or any(type(row[key]) is not bool for key in ('asynchronous', 'expanded'))
            or row['file'] not in inputs):
        raise ValueError('invalid Rust lifetime source binding')
    relative = Path(row['file']).relative_to(root).as_posix()
    if (row['identity'] != relative + '::' + row['compiler_name']
            or not 0 <= number(row['start'], len(inputs[row['file']]) + 1, 'source start')
            < number(row['end'], len(inputs[row['file']]) + 1, 'source end')):
        raise ValueError('invalid Rust lifetime source range/identity')
    return relative


def value(row: Any, body: dict, root: Path, inputs: dict[str, bytes]) -> dict:
    """Validate actual local type/layout facts, including unavailable layouts."""
    row = closed(row, VALUE_FIELDS, 'value')
    source(row['source'], root, inputs)
    local = number(row['local'], body['local_count'], 'local')
    role = 'local'
    if local == 0:
        role = 'return'
    elif local <= body['argument_count']:
        role = 'argument'
    if (row['role'] != role or row['kind'] not in ('struct', 'enum', 'union')
            or type(row['needs_drop']) is not bool or type(row['rendered_type']) is not str
            or not row['rendered_type'] or row['compiler_type'] != row['source'].get('compiler_name')):
        raise ValueError('invalid Rust lifetime type/role')
    if row['layout_error'] is None:
        number(row['size'], 2**64, 'type size')
        alignment = number(row['alignment'], 2**64, 'type alignment')
        if not alignment or alignment & (alignment - 1):
            raise ValueError('invalid Rust lifetime alignment')
    elif (type(row['layout_error']) is not str or not row['layout_error']
          or row['size'] is not None or row['alignment'] is not None):
        raise ValueError('invalid Rust lifetime unavailable layout')
    return row


def site(row: Any, body: dict, locals_: set[int]) -> None:
    """Retain root-local ownership sites without claiming projected-field coverage."""
    row = closed(row, SITE_FIELDS, 'site')
    if (type(row['kind']) is not str or row['kind'] not in SITE_KINDS
            or type(row['local']) is not int or row['local'] not in locals_
            or any(type(row[key]) is not bool for key in ('projected', 'cleanup'))
            or (row['unwind'] is not None and type(row['unwind']) is not str)
            or (row['kind'] == 'drop') != (type(row['unwind']) is str and bool(row['unwind']))):
        raise ValueError('invalid Rust lifetime site binding')
    number(row['block'], body['block_count'], 'site block')
    if row['statement'] is not None:
        number(row['statement'], 2**32, 'site statement')
    if row['other'] is not None:
        number(row['other'], body['local_count'], 'site destination')


def unavailable_value(row: Any, body: dict) -> int:
    """Keep unsupported root values visible without inventing declaration origins."""
    row = closed(row, UNAVAILABLE_FIELDS, 'unavailable value')
    local = number(row['local'], body['local_count'], 'unavailable local')
    role = 'return' if local == 0 else 'argument' if local <= body['argument_count'] else 'local'
    if (row['role'] != role or type(row['needs_drop']) is not bool
            or type(row['kind']) is not str or row['kind'] not in ('foreign_adt', 'array', 'tuple', 'opaque')
            or any(type(row[key]) is not str or not row[key] for key in ('compiler_type', 'rendered_type'))):
        raise ValueError('invalid Rust lifetime unavailable type/role')
    return local


def body(row: Any, root: Path, inputs: dict[str, bytes], version: int) -> dict:
    """Reject malformed unselected bodies before applying operator exclusions."""
    fields = {'function', 'phase', 'local_count', 'block_count', 'argument_count', 'locals', 'sites'}
    row = closed(row, fields | ({'unavailable_locals'} if version == 2 else set()), 'body')
    source(row['function'], root, inputs)
    if (row['phase'] != 'Runtime(PostCleanup)' or type(row['locals']) is not list or len(row['locals']) > 65536
            or type(row['sites']) is not list or len(row['sites']) > 262144):
        raise ValueError('invalid Rust lifetime phase/bounds')
    number(row['local_count'], 65537, 'local count'); number(row['block_count'], 65537, 'block count')
    if not row['block_count']:
        raise ValueError('invalid Rust lifetime empty body')
    number(row['argument_count'], row['local_count'], 'argument count')
    locals_ = [value(item, row, root, inputs)['local'] for item in row['locals']]
    if len(locals_) != len(set(locals_)) or row['function']['asynchronous'] or row['function']['expanded']:
        raise ValueError('invalid Rust lifetime duplicate local or unsupported body')
    if version == 2:
        unsupported = row['unavailable_locals']
        if type(unsupported) is not list or len(unsupported) > 65536:
            raise ValueError('invalid Rust lifetime unavailable locals')
        indices = [unavailable_value(item, row) for item in unsupported]
        if len(indices) != len(set(indices)) or set(indices).intersection(locals_):
            raise ValueError('invalid Rust lifetime overlapping unavailable locals')
    for item in row['sites']:
        site(item, row, set(locals_))
    return row


def original_inputs(root: Path, sources: dict[str, str]) -> dict[str, bytes]:
    """Recheck bounded canonical original inputs before consuming discovery."""
    if not 1 <= len(sources) <= 65536:
        raise ValueError('Rust lifetime discovery requires bounded original inputs')
    inputs = {}
    for name, expected in sources.items():
        filename = Path(name)
        if filename.resolve(strict=True) != filename or not filename.is_relative_to(root):
            raise ValueError('Rust lifetime input is outside the original canonical root')
        raw = filename.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Rust lifetime original source changed')
        inputs[name] = raw
    return inputs


def inventory(path: Path, root: Path, sources: dict[str, str], inputs: dict[str, bytes]) -> dict:
    """Validate every body, including excluded metadata, before selection."""
    _, raw = checked_file(path, nofollow=True, include_content=True, max_bytes=8 * 1024 * 1024)
    document = closed(decode(raw), {'schema_version', 'kind', 'sources', 'target', 'pointer_width', 'bodies',
                                   'skipped', 'source_complete', 'instrumentation_complete'}, 'inventory')
    if (type(document['schema_version']) is not int or document['schema_version'] not in (1, 2)
            or document['kind'] != 'otelc.rust.lifetime-inventory' or document['sources'] != sources
            or type(document['pointer_width']) is not int or document['pointer_width'] != 64
            or type(document['target']) is not str or not document['target']
            or document['source_complete'] is not False or document['instrumentation_complete'] is not False
            or type(document['bodies']) is not list or len(document['bodies']) > 65536
            or type(document['skipped']) is not list or len(document['skipped']) > 65536):
        raise ValueError('Rust lifetime inventory does not match the original discovery profile')
    identities = set()
    for row in document['bodies']:
        body(row, root, inputs, document['schema_version'])
        identity = row['function']['identity']
        if identity in identities:
            raise ValueError('duplicate Rust lifetime body')
        identities.add(identity)
    for item in document['skipped']:
        skipped = closed(item, {'function', 'reason'}, 'skipped body')
        source(skipped['function'], root, inputs)
        identity = skipped['function']['identity']
        if skipped['reason'] not in ('const', 'async', 'closure', 'expanded') or identity in identities:
            raise ValueError('invalid Rust lifetime skipped reason')
        identities.add(identity)
    return document


def _read(path: Path, policy: Policy, root: Path, sources: dict[str, str]) -> tuple[dict, dict]:
    """Read one discovery product and select types using the schema-3 TOML policy."""
    if 'rust' not in policy.data['languages'] or root.resolve(strict=True) != root:
        raise ValueError('Rust lifetime discovery requires Rust and a canonical source root')
    inputs = original_inputs(root, sources)
    document = inventory(path, root, sources, inputs)
    selected = []
    source_selector, type_selector = policy.selection('sources'), policy.selection('lifetimes')
    type_exclusions = Selection(type_selector.exclude, (), paths=type_selector.paths)
    for row in document['bodies']:
        include = policy.data['lifetimes']['enabled'] and source_selector.accepts(source(row['function'], root, inputs))
        locals_ = [item for item in row['locals'] if include
                   and source_selector.accepts(source(item['source'], root, inputs))
                   and type_selector.accepts(item['rendered_type'])]
        unsupported = [item for item in row.get('unavailable_locals', []) if include
                       and not any(type_exclusions.accepts(item[key]) for key in ('compiler_type', 'rendered_type'))
                       and any(type_selector.accepts(item[key]) for key in ('compiler_type', 'rendered_type'))]
        if locals_ or unsupported:
            indexes = {item['local'] for item in locals_}
            selected.append(row | {'locals': locals_, 'sites': [item for item in row['sites'] if item['local'] in indexes]}
                            | ({'unavailable_locals': unsupported} if document['schema_version'] == 2 else {}))
    selection = {key: policy.data[key] for key in ('sources', 'lifetimes')}
    return document, {'schema_version': document['schema_version'], 'kind': 'otelc.rust.lifetime-selection', 'bodies': selected,
        'selection_sha256': hashlib.sha256(json.dumps(selection, sort_keys=True).encode()).hexdigest(),
        'target': document['target'], 'pointer_width': document['pointer_width'],
        'source_complete': False, 'instrumentation_complete': False, 'skipped': document['skipped']}


def read(path: Path, policy: Policy, root: Path, sources: dict[str, str]) -> dict:
    """Read one discovery product and apply the common source/type selectors."""
    return _read(path, policy, root, sources)[1]


def _locals(selected: dict) -> dict[str, dict[str, str]]:
    for body in selected['bodies']:
        if body.get('unavailable_locals'):
            names = ', '.join(item['rendered_type'] for item in body['unavailable_locals'])
            raise ValueError('selected Rust lifetime type is unavailable: ' + names)
    return {body['function']['identity']: {str(row['local']): row['compiler_type']
            for row in body['locals']} for body in selected['bodies']}


def weave_bindings(path: Path, policy: Policy, root: Path, sources: dict[str, str]) -> dict[str, dict[str, str]]:
    """Bind common-policy selections to original compiler locals for private weaving.

    Keep unsupported argument/layout/drop shapes in the request: the compiler
    must refuse them, rather than quietly instrumenting a smaller selection.
    """
    return _locals(read(path, policy, root, sources))


def weave_request(path: Path, policy: Policy, root: Path, sources: dict[str, str]) -> dict:
    """Bind locals and complete lifetime MIR bodies from one checked discovery.

    The compiler rehashes its own complete snapshot before adding any hooks.
    Compact sorted UTF-8 JSON matches serde_json Value object ordering; the
    snapshot schema contains integer positions/layouts and no floating values.
    """
    document, selected = _read(path, policy, root, sources)
    bindings = _locals(selected)
    bodies = {body['function']['identity']: hashlib.sha256(json.dumps(body, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        for body in document['bodies'] if body['function']['identity'] in bindings}
    return {'schema_version': 2, 'lifetimes': bindings, 'lifetime_bodies': bodies}
