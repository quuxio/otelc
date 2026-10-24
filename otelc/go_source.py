"""Resolve original compiler-selected Go declarations against the common policy."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from ci import build_go_upstream as tooling
from otelc.config import Policy, Selection
from otelc.file_inputs import checked_file

ROOT = Path(__file__).resolve().parents[1]
MAX_INVENTORY = 32 * 1024 * 1024


def identity(path: Path) -> dict[str, Any]:
    """Bind exact regular-file bytes using the shared input checker."""
    value, _ = checked_file(path, nofollow=True, max_bytes=MAX_INVENTORY)
    return value


def inspect_project(root: Path, target: str, tools: dict[str, Any],
                    output: Path) -> dict[str, Any]:
    """Build the locked inspector and load types without executing the target."""
    root = root.absolute()
    if root.resolve(strict=True) != root:
        raise ValueError('Go source root must be canonical without symlink aliases')
    if (root / 'go.work').exists() or (root / 'vendor').exists():
        raise ValueError('Go workspace/vendor projects require a separate build profile')
    original = {name: identity(root / name) for name in ('go.mod', 'go.sum')
                if (root / name).exists()}
    if 'go.mod' not in original:
        raise ValueError('Go source project requires an original module manifest')
    source = ROOT / 'adapters/go-source'
    tooling_inputs = {p.name: identity(p) for p in source.iterdir()
                      if p.name in ('go.mod', 'go.sum', 'main.go')}
    output = output.resolve()
    if output.is_relative_to(root):
        raise ValueError('Go tooling output must be outside original application inputs')
    output.mkdir(mode=0o700)
    env = tooling.compiler_environment(tools, ROOT / 'build/go-upstream')
    env.update(GOPACKAGESDRIVER='off')
    binary = output / 'inspect'
    build = subprocess.run([tools['go'], 'build', '-mod=readonly', '-trimpath', '-o', str(binary), '.'],
                           cwd=source, env=env, capture_output=True, timeout=300)
    (output / 'build.stdout').write_bytes(build.stdout)
    (output / 'build.stderr').write_bytes(build.stderr)
    if build.returncode:
        raise RuntimeError(f'Go source inspector build failed ({build.returncode})')
    request = {'schema_version': 1, 'root': str(root), 'target': target}
    result = subprocess.run([str(binary)], input=json.dumps(request).encode(), cwd=output,
                            env=env, capture_output=True, timeout=300)
    (output / 'inspect.stderr').write_bytes(result.stderr)
    if result.returncode or len(result.stdout) > MAX_INVENTORY:
        raise ValueError('Go source inspection failed: ' + result.stderr.decode(errors='replace')[-4096:])
    inventory = json.loads(result.stdout)
    if (type(inventory) is not dict or inventory.get('schema_version') != 1
            or inventory.get('root') != str(root) or inventory.get('target') != target):
        raise ValueError('Go source inspector returned a different request binding')
    for name, sha256 in inventory['files'].items():
        path = root / name
        if not path.is_relative_to(root) or '..' in Path(name).parts or path.resolve(strict=True) != path:
            raise ValueError('Go source inventory escaped the original module')
        if identity(path)['sha256'] != sha256:
            raise ValueError('Go source changed after compiler type inspection')
    if any(identity(root / name) != value for name, value in original.items()):
        raise ValueError('Go module inputs changed during source inspection')
    if any(identity(source / name) != value for name, value in tooling_inputs.items()):
        raise ValueError('Go inspector tooling changed during source inspection')
    (output / 'inventory.json').write_bytes(result.stdout)
    receipt = {'schema_version': 1, 'kind': 'otelc.go-source-inspection',
               'request': request, 'inventory': identity(output / 'inventory.json'),
               'inspector': identity(binary), 'tooling': tooling_inputs, 'modules': original,
               'go': {'path': tools['go'], 'sha256': tools['go_sha256']},
               'source_complete': False}
    (output / 'inspection.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
    return inventory


def resolve(policy: Policy, inventory: dict[str, Any]) -> dict[str, Any]:
    """Select functions, concrete allocations and releases without fixture names."""
    if 'go' not in policy.data['languages']:
        raise ValueError('Go is not enabled by the common policy')
    if inventory.get('schema_version') != 1:
        raise ValueError('unsupported Go source inventory')
    sources = policy.selection('sources')

    def selected(row: dict[str, Any], section: str) -> bool:
        return sources.accepts(row['file']) and policy.selection(section).accepts(row['name'])

    functions = [row for row in inventory['functions'] if selected(row, 'functions')]
    types = [row for row in inventory['types'] if policy.data['lifetimes']['enabled']
             and selected(row, 'lifetimes')]
    if len(functions) > 4096 or len(types) > 4096:
        raise ValueError('resolved Go bindings exceed the owned runtime capacity')
    if len({row['name'] for row in functions}) != len(functions):
        raise ValueError('ambiguous selected Go declaration identity')
    for row in [*functions, *types]:
        if row['gap']:
            raise ValueError(f'unsupported selected Go binding {row["name"]}: {row["gap"]}')
    type_ids = {row['name']: index for index, row in enumerate(types)}
    allocations = [dict(row, lifetime=type_ids[row['type']]) for row in inventory['allocations']
                   if row['type'] in type_ids and sources.accepts(row['file'])]
    for row in allocations:
        if row['gap']:
            raise ValueError(f'unsupported selected Go allocation {row["file"]}:{row["line"]}: {row["gap"]}')
    releases = []
    methods = Selection(tuple(policy.data['lifetimes']['release_methods']), ())
    for row in inventory['functions']:
        receiver = row['receiver_id']
        name = row['package'] + '.' + receiver.removeprefix('*')
        if name not in type_ids or not sources.accepts(row['file']) or not methods.accepts(row['symbol']):
            continue
        if (row['gap'] or not receiver.startswith('*') or row['generic']
                or row['receiver'] in ('', '_') or row['parameters'] or row['variadic']
                or len(row['results']) > 1 or row['results'] and not row['results'][0]['error']):
            raise ValueError(f'unsupported selected Go release method {row["name"]}')
        releases.append(dict(row, lifetime=type_ids[name]))
    value = {'schema_version': 1, 'kind': 'otelc.go-source-selection', 'main': inventory['main'],
             'functions': functions, 'types': types, 'allocations': allocations, 'releases': releases,
             'context': policy.data['context'].copy(), 'source_complete': False}
    value['sha256'] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return value
