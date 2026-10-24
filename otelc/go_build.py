"""Build stock and privately woven Go binaries from one checked source recipe."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from ci import build_go_upstream as tooling
from otelc import go_context_bundle, go_http_bundle, go_source
from otelc.config import Policy

SOURCE_SUFFIXES = frozenset(('.go', '.s', '.S', '.h', '.syso', '.c', '.cc', '.cpp', '.cxx', '.m', '.mm', '.f', '.f90'))


def source_roster(root: Path) -> list[str]:
    """New source/native units require rebuilding; ordinary runtime logs do not."""
    return sorted(path.relative_to(root).as_posix() for path in root.rglob('*')
                  if '.git' not in path.relative_to(root).parts
                  and path.suffix in SOURCE_SUFFIXES and path.is_file())


def documents(raw: bytes) -> list[dict[str, Any]]:
    """Read cmd/go's concatenated JSON module records without line assumptions."""
    text = raw.decode()
    decoder = json.JSONDecoder()
    values = []
    while text.strip():
        value, end = decoder.raw_decode(text.lstrip())
        if type(value) is not dict or not value.get('Path'):
            raise ValueError('invalid Go module identity')
        values.append(value)
        text = text.lstrip()[end:]
    if not values or len({row['Path'] for row in values}) != len(values):
        raise ValueError('empty or repeated Go module graph')
    return values


def preserve_modules(before: list[dict[str, Any]], after: list[dict[str, Any]],
                     settings: dict[str, Any], updated: dict[str, Any]) -> None:
    """SDK requirements may add modules, never change an existing resolution."""
    if (settings.get('Go', '').split('.')[:2] != updated.get('Go', '').split('.')[:2]
            or any(settings.get(key) != updated.get(key) for key in ('Toolchain', 'Godebug'))):
        raise ValueError('instrumentation would change Go language/toolchain/GODEBUG settings')
    resolved = {row['Path']: row for row in after}
    for row in before:
        match = resolved.get(row['Path'])
        if match is None or any(row.get(key) != match.get(key) for key in ('Version', 'Main', 'Replace')):
            raise ValueError('instrumentation would change existing Go dependency: ' + row['Path'])
        if not row.get('Main') and any(row.get(key) and row[key] != match.get(key)
                                       for key in ('Sum', 'GoModSum', 'GoVersion')):
            raise ValueError('instrumentation would change Go dependency identity: ' + row['Path'])


def inputs(root: Path) -> dict[str, dict[str, Any]]:
    """Freeze ordinary project files; private products must live outside root."""
    files = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if '.git' in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError('Go source profile requires ordinary files without project symlinks')
        if path.is_dir():
            continue
        files[relative.as_posix()] = go_source.identity(path)
        if len(files) > 4096:
            raise ValueError('Go project exceeds4096 input files in this build profile')
    return files


def allocate(source: bytes, rows: list[dict[str, Any]], prefix: str) -> bytes:
    """Wrap exact typed expressions; ordered inserts preserve nested evaluation."""
    events: dict[int, list[tuple[int, int, bytes]]] = {}
    for row in rows:
        start, end = row['start'], row['end']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source):
            raise ValueError('invalid Go allocation source offsets')
        events.setdefault(start, []).append((1, -end, (prefix + 'Track(').encode()))
        events.setdefault(end, []).append((0, -start, (', ' + str(row['lifetime']) + ')').encode()))
    chunks, previous = [], 0
    for offset, inserts in sorted(events.items()):
        chunks.append(source[previous:offset])
        chunks.extend(text for _, _, text in sorted(inserts))
        previous = offset
    return b''.join([*chunks, source[previous:]])


def runtime_factory(prefix: str, selection_sha256: str, http: bool) -> str:
    """Use one initializer body for direct helpers and eager HTTP bootstrap."""
    configure = (f' if err:={prefix}httptrace.Configure(value.TracerProvider(),value.Metrics.Provider,'
                 f'{prefix}official.ContextWithImplicitTelemetry,{prefix}official.DetachSpanFromGLS);err!=nil{{panic(err)}}\n') if http else ''
    return f'''var {prefix}Runtime = func()*{prefix}owned.Runtime {{
 value,err:={prefix}owned.FromEnvironment({prefix}official.ContextWithImplicitTelemetry,{prefix}official.DetachSpanFromGLS)
 if err!=nil{{panic(err)}}
 if value.Config.SelectionSHA256!={json.dumps(selection_sha256)}{{panic("Go runtime differs from compiled selection")}}
{configure}\
 return value
}}()
'''


def helper(package: str, prefix: str, selection_sha256: str, *, bootstrap: str | None = None) -> str:
    """Generated bootstrap is private tooling, never developer-authored code."""
    imports = (' ' + prefix + 'bootstrap ' + json.dumps(bootstrap) + '\n') if bootstrap else (
        f' {prefix}official "go.opentelemetry.io/otelc/pkg/runtime"\n {prefix}owned "io.quux.otelc/go/runtime"\n')
    initialization = f'var {prefix}Runtime = {prefix}bootstrap.Runtime\n' if bootstrap else runtime_factory(prefix, selection_sha256, False)
    return f'''package {package}
import (
 {prefix}probe "io.quux.otelc/go/probe"
{imports}\
)
{initialization}\
func {prefix}Track[T any](value *T,id int)*T{{
 {prefix}probe.Track({prefix}Runtime.Probes,value,id);return value
}}
func {prefix}Release[T any](value *T,success bool){{{prefix}probe.Release({prefix}Runtime.Probes,value,success)}}
'''


def rules(selection: dict[str, Any], inventory: dict[str, Any], prefix: str) -> str:
    """One rule per declaration composes shutdown, function and release hooks."""
    function_ids = {row['name']: index for index, row in enumerate(selection['functions'])}
    releases = {row['name']: row for row in selection['releases']}
    rows = []
    for row in inventory['functions']:
        finish = row['package'] == inventory['main'] and row['symbol'] == 'main' and not row['receiver_id']
        if row['name'] not in function_ids and row['name'] not in releases and not finish:
            continue
        code = []
        if finish:
            code.append('defer ' + prefix + 'Runtime.Close()')
        failure = ' || '.join(value['failure'] for value in row['results'] if value['error']) or 'false'
        if row['name'] in function_ids:
            code.append(f'{prefix}Invocation := {prefix}Runtime.Probes.Start({function_ids[row["name"]]})')
            code.append('defer func(){' + prefix + 'Invocation.End(' + failure + ')}()')
        if row['name'] in releases:
            code.append(f'{prefix}Completion := {prefix}Runtime.Probes.Watch()')
            code.append('defer func(){' + prefix + 'Release(' + row['receiver'] + ','
                        + prefix + 'Completion.End(' + failure + '))}()')
        target = 'main' if row['package'] == inventory['main'] else row['package']
        rule = f'rule_{len(rows)}:\n  target: {json.dumps(target)}\n  where:\n    func: {json.dumps(row["symbol"])}\n'
        if row['receiver_id']:
            rule += '    recv: ' + json.dumps(row['receiver_id']) + '\n'
        rule += '  do:\n    - inject_code:\n        raw: |\n' + ''.join('          ' + line + '\n' for line in code)
        rows.append(rule)
    return '\n'.join(rows)


def instrument_sources(root: Path, private: Path, inventory: dict[str, Any],
                       selection: dict[str, Any], module_name: str) -> None:
    """Materialise typed expression wrappers, package helpers and upstream rules."""
    prefix = '__otelc_' + selection['sha256'][:12] + '_'
    selected = {row['name'] for row in [*selection['functions'], *selection['releases']]}
    by_package = {row['package'] for row in inventory['functions']
                  if row['name'] in selected or row['package'] == inventory['main'] and row['symbol'] == 'main'}
    bootstrap = None
    if selection['context']['http']:
        by_package.update(inventory['packages'])
        name = 'otelc_bootstrap_' + selection['sha256'][:12]
        directory = private / name
        if directory.exists():
            raise ValueError('Go HTTP bootstrap would replace an application directory')
        directory.mkdir()
        bootstrap = module_name + '/' + name
        (directory / 'bootstrap.go').write_text('package otelcbootstrap\nimport (\n'
            ' official "go.opentelemetry.io/otelc/pkg/runtime"\n owned "io.quux.otelc/go/runtime"\n'
            ' httptrace "io.quux.otelc/go-http"\n)\n' + runtime_factory('', selection['sha256'], True))
    for row in selection['allocations']:
        package = next(name for name, fact in inventory['packages'].items()
                       if fact['directory'] == Path(row['file']).parent.as_posix())
        by_package.add(package)
    for name in inventory['files']:
        if prefix.encode() in (root / name).read_bytes():
            raise ValueError('generated Go hook identifier conflicts with original source')
    for package in sorted(by_package):
        fact = inventory['packages'][package]
        directory = private / fact['directory']
        name = 'zz_otelc_' + selection['sha256'][:12] + '.go'
        if (directory / name).exists():
            raise ValueError('generated Go helper would replace an application file')
        (directory / name).write_text(helper(fact['name'], prefix, selection['sha256'], bootstrap=bootstrap))
    for name in inventory['files']:
        rows = [row for row in selection['allocations'] if row['file'] == name]
        if rows:
            (private / name).write_bytes(allocate((root / name).read_bytes(), rows, prefix))
    rule_dir = private / 'otelc_rules'
    if rule_dir.exists() or (private / 'otel.instrumentation.go').exists() or (private / 'otelc.tool.go').exists():
        raise ValueError('existing Go instrumentation requires a separate composition profile')
    rule_dir.mkdir()
    (rule_dir / 'doc.go').write_text('package rules\n')
    (rule_dir / 'otelc.yaml').write_text(rules(selection, inventory, prefix))
    instruments = [module_name + '/otelc_rules',
            'go.opentelemetry.io/otelc/instrumentation/runtime',
            'go.opentelemetry.io/otelc/instrumentation/go.opentelemetry.io/otel/sdk/trace']
    if selection['context']['http']:
        instruments.append('io.quux.otelc/go-http')
    (private / 'otel.instrumentation.go').write_text('//go:build tools\n\npackage tools\nimport (\n'
        + ''.join(' _ ' + json.dumps(name) + '\n' for name in instruments) + ')\n')


def build_project(root: Path, target: str, policy: Policy, output: Path,
                  tools: dict[str, Any]) -> dict[str, Any]:
    """Produce both immutable binaries; launching is a separate compiler-free API."""
    root, output = root.absolute(), output.resolve()
    if root.resolve(strict=True) != root or output.is_relative_to(root):
        raise ValueError('Go build requires a canonical root and external output')
    original = inputs(root)
    output.mkdir(mode=0o700)
    inventory = go_source.inspect_project(root, target, tools, output / 'inspection')
    if any(name not in original or original[name]['sha256'] != digest for name, digest in inventory['compiler_inputs'].items()):
        raise ValueError('Go parser input differs from the frozen original project')
    selection = go_source.resolve(policy, inventory)
    private = output / 'private'
    shutil.copytree(root, private, ignore=shutil.ignore_patterns('.git'))
    copied = inputs(private)
    if ({name: value['sha256'] for name, value in copied.items()}
            != {name: value['sha256'] for name, value in original.items()}):
        raise ValueError('private Go copy differs from frozen original inputs')
    sdk = output / 'sdk'
    shutil.copytree(go_source.ROOT / 'adapters/go', sdk)
    sdk_inputs = inputs(sdk)
    modules = output / 'context'
    go_context_bundle.prepare(Path(tools['bundle']), modules, tasks=policy.data['context']['tasks'])
    env = tooling.compiler_environment(tools, go_source.ROOT / 'build/go-upstream')
    scratch = output / 'compiler-temp'
    scratch.mkdir()
    env.update(GOTMPDIR=str(scratch), TMPDIR=str(scratch), GOPACKAGESDRIVER='off')
    commands = []

    def command(argv: list[str], cwd: Path) -> bytes:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=600)
        index = len(commands)
        for suffix, raw in (('stdout', result.stdout), ('stderr', result.stderr)):
            (output / f'command-{index}.{suffix}').write_bytes(raw)
        commands.append({'argv': argv, 'cwd': str(cwd), 'exit_code': result.returncode})
        (output / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        if result.returncode:
            raise ValueError(f'Go source build command{index} failed: ' + result.stderr.decode(errors='replace')[-4096:])
        return result.stdout

    go = tools['go']
    settings = json.loads(command([go, 'mod', 'edit', '-json'], root))
    if any(not row['New'].get('Version') for row in settings.get('Replace') or []):
        raise ValueError('local Go module replacements require a separate closure profile')
    before = documents(command([go, 'list', '-m', '-mod=readonly', '-json', 'all'], root))
    instrument_sources(root, private, inventory, selection, settings['Module']['Path'])
    http_modules = {}
    if policy.data['context']['http']:
        http_directory = private / '.otelc-http'
        if http_directory.exists():
            raise ValueError('Go HTTP support directory conflicts with an application input')
        http_directory.mkdir(mode=0o700)
        hooks = http_directory / 'hooks'
        shutil.copytree(go_source.ROOT / 'adapters/go-http', hooks)
        upstream = http_directory / 'otelhttp'
        go_http_bundle.prepare(tools, upstream)
        http_modules = {'io.quux.otelc/go-http': hooks, go_http_bundle.MODULE: upstream}
        native = Path(tools['go']).parent.parent / 'src/net/http'
        for name, declaration in (('roundtrip.go', 'func (t *Transport) RoundTrip(req *Request) (*Response, error) {'),
                                  ('server.go', 'func (sh serverHandler) ServeHTTP(rw ResponseWriter, req *Request) {')):
            if (native / name).read_text().count(declaration) != 1:
                raise ValueError('Go HTTP native entry differs from the pinned compiler profile')
    for name, path in {**go_context_bundle.replacements(modules), 'io.quux.otelc/go': sdk, **http_modules}.items():
        command([go, 'mod', 'edit', '-replace=' + name + '=' + str(path), '-require=' + name + '@v0.0.0'], private)
    command([go, 'mod', 'edit', '-require=go.opentelemetry.io/otel/sdk@v1.47.0'], private)
    command([go, 'mod', 'tidy'], private)
    after = documents(command([go, 'list', '-m', '-mod=readonly', '-json', 'all'], private))
    preserve_modules(before, after, settings, json.loads(command([go, 'mod', 'edit', '-json'], private)))
    command([go, 'mod', 'verify'], root)
    command([go, 'mod', 'verify'], private)
    plain, instrumented = output / 'plain', output / 'instrumented'
    command([go, 'build', '-mod=readonly', '-o', str(plain), target], root)
    command([tools['weaver'], '--work-dir', str(output / 'weaver-work'), 'go', 'build',
             '-mod=readonly', '-o', str(instrumented), target], private)
    if inputs(root) != original or inputs(sdk) != sdk_inputs:
        raise ValueError('Go original application or private SDK changed during compilation')
    receipt = {'schema_version': 4, 'kind': 'otelc.go-source-build', 'root': str(root), 'target': target,
               'runtime_protocols': {'config': 2, 'health': 4},
               'selection': selection, 'inputs': original, 'sdk_inputs': sdk_inputs,
               'launch_inputs': {name: original[name] for name in
                                 sorted(set(inventory['compiler_inputs']) | {'go.mod'} | ({'go.sum'} & set(original)))},
               'source_roster': source_roster(root),
               'private_inputs': inputs(private), 'context_inputs': inputs(modules),
               'inventory': go_source.identity(output / 'inspection/inventory.json'),
               'plain': go_source.identity(plain), 'instrumented': go_source.identity(instrumented),
               'modules_before': before, 'modules_after': after, 'settings': settings,
               'toolchain': tools, 'commands': commands, 'source_complete': False}
    (output / 'build.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
    shutil.rmtree(scratch)
    return receipt
