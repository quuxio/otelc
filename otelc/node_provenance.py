"""Retain source-launch inputs separately from any claim about action delivery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from otelc.config import Selection
from otelc.file_inputs import checked_file, identity
from otelc import node_recipe

TOOLS_DIRECTORY = Path('build/tools')


def inventory(directory: Path, *, suffix: str | None = None) -> list[dict[str, Any]]:
    """Inventory complete producer directories without silently skipping errors."""
    if not directory.is_dir():
        raise ValueError(f'launch tooling directory is missing: {directory}')
    records = []
    for current, directories, files in os.walk(directory, onerror=raise_walk_error):
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise ValueError('symlinked producer directories are unsupported')
        records.extend(identity(Path(current) / name) for name in sorted(files)
                       if suffix is None or name.endswith(suffix))
    return sorted(records, key=lambda item: item['path'])


def raise_walk_error(error: OSError) -> None:
    """An unreadable tooling subtree is an incomplete inventory, not an empty one."""
    raise error


def package_context(application: Path) -> list[dict[str, Any]]:
    """Bind the nearest package and all nearer absences that affect Node format."""
    records = []
    directory = application.parent
    while True:
        filename = directory / 'package.json'
        if os.path.lexists(filename):
            records.append(identity(filename))
            return records
        records.append({'path': str(filename), 'absent': True})
        if directory.parent == directory:
            return records
        directory = directory.parent


def launcher_inputs(producer: Path) -> list[dict[str, Any]]:
    """Bind producer Python source without unstable bytecode caches."""
    return inventory(producer / 'otelc', suffix='.py')


def capture(producer: Path, application: Path, selected: str, plan: dict[str, Any],
            generated: list[Path], argv: list[str], original: dict[str, Any]) -> dict[str, Any]:
    """Capture the actual installed closure, including optional platform packages."""
    environment = node_recipe.tooling_environment()
    runtime = json.loads(subprocess.check_output([selected, '-p',
        'JSON.stringify({version:process.versions.node,abi:process.versions.modules,executable:process.execPath,architecture:process.arch,platform:process.platform})'],
        text=True, env=environment))
    tooling = {'launcher': launcher_inputs(producer)}
    if not (plan['plain'] and plan['language'] == 'javascript'):
        tooling['adapter'] = inventory(producer / 'adapters/node/src')
        tooling['packages'] = inventory(producer / 'node_modules')
        tooling['package_metadata'] = [identity(producer / name) for name in ('package.json', 'package-lock.json')]
        if not plan['plain']:
            tooling['observer'] = [identity(producer / TOOLS_DIRECTORY / f"otelc_node_observer-{runtime['version']}.node")]
        if not plan['plain'] and plan['context']['http']:
            manifest = producer / TOOLS_DIRECTORY / 'otelc-http.json'
            product = json.loads(manifest.read_text())
            tooling['http'] = [identity(manifest), *inventory(producer / TOOLS_DIRECTORY / product['directory'])]
    return {'schema_version': 3, 'request': original, 'launch_id': plan['launch_id'], 'language': plan['language'],
        'backend': plan['backend'], 'plain': plan['plain'], 'root': plan['runtime']['root'],
        'argv': argv, 'cwd': str(Path.cwd().resolve()), 'launcher_pid': os.getpid(),
        'node': identity(Path(runtime.pop('executable')).resolve(strict=True)), 'runtime': runtime,
        'entry': identity(application), 'plan_sha256': hashlib.sha256(
            json.dumps(plan, sort_keys=True).encode()).hexdigest(),
        'resolved_plan': plan,
        'tooling_environment': node_recipe.environment_snapshot(environment),
        'ambient_sha256': {key: hashlib.sha256(os.environ[key].encode()).hexdigest()
            for key in ('NODE_OPTIONS', 'NODE_PATH') if key in os.environ},
        'tooling': tooling, 'generated': [identity(filename) for filename in generated],
        'entry_context': package_context(application)}


def verify_tooling(expected: dict[str, Any]) -> None:
    """Recheck file sets as well as bytes, including dependency additions/removals."""
    producer = Path(__file__).resolve().parent.parent
    for group, items in expected['tooling'].items():
        if group == 'launcher':
            actual = launcher_inputs(producer)
        elif group in ('adapter', 'packages'):
            directory = producer / ('adapters/node/src' if group == 'adapter' else 'node_modules')
            actual = inventory(directory)
        elif group == 'http':
            product = json.loads(Path(items[0]['path']).read_text())
            actual = [identity(Path(items[0]['path'])), *inventory(producer / TOOLS_DIRECTORY / product['directory'])]
        else:
            actual = [identity(Path(item['path'])) for item in items]
        if actual != items:
            raise ValueError('launch tooling changed: ' + group)


def verify_emission(runtime: Path, prefix: str, record: dict[str, Any],
                    sources: set[tuple[str, str]]) -> None:
    """Validate an output against a preceding original source and retained bytes."""
    if (record['path'], record['source_sha256']) not in sources:
        raise ValueError('emitted module has no original source binding')
    retained_artifact(runtime, prefix, {'file': record['output_file'], 'sha256': record['sha256'], 'size': record['size']})
    if 'map_file' in record:
        retained_artifact(runtime, prefix, {'file': record['map_file'], 'sha256': record['map_sha256'], 'size': record['map_size']})


def retained_artifact(runtime: Path, prefix: str, artifact: dict[str, Any]) -> bytes:
    """Read only content-addressed compiler files from this launch namespace."""
    digest = artifact.get('sha256')
    if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
        raise ValueError('invalid compiler artifact digest')
    filename = runtime / (prefix + '-module-' + digest + '.txt')
    if artifact['file'] != str(filename):
        raise ValueError('compiler artifact is outside the generated launch namespace')
    actual, content = checked_file(filename, include_content=True, nofollow=True)
    if actual['sha256'] != digest or actual['size'] != artifact['size']:
        raise ValueError('retained compiler artifact changed')
    return content


def compiler_files(runtime: Path, prefix: str, compiler: dict[str, Any]) -> dict[str, bytes]:
    """Keep raw compiler inputs, options, outputs and maps as distinct artifacts."""
    required = {'input', 'options', 'output', 'map'}
    if compiler['backend'] == 'native':
        required.add('preparation_map')
    if set(compiler['files']) != required:
        raise ValueError('compiler artifact set is incomplete or unknown')
    return {role: retained_artifact(runtime, prefix, artifact) for role, artifact in compiler['files'].items()}


def verify_compiler_identity(compiler: dict[str, Any], expected: dict[str, Any]) -> None:
    """Compiler metadata can identify the fixed producer tool, never choose a command."""
    producer = Path(__file__).resolve().parent.parent
    backend = expected['backend']
    if compiler['backend'] != backend:
        raise ValueError('compiler backend differs from the resolved plan')
    if backend == 'api':
        filename = producer / 'node_modules/typescript-api/lib/typescript.js'
        version = '6.0.3'
        if compiler.get('method') != 'transpileModule' or 'argv' in compiler:
            raise ValueError('unknown API compiler invocation')
    else:
        platform = expected['runtime']['platform'] + '-' + expected['runtime']['architecture']
        filename = producer / 'node_modules/@typescript' / ('typescript-' + platform) / 'lib/tsc'
        version = '7.0.2'
        if compiler.get('argv') != [str(filename.resolve(strict=True)), '--project', compiler['files']['options']['path']]:
            raise ValueError('unknown native compiler invocation')
    if compiler['compiler_path'] != str(filename.resolve(strict=True)) or compiler['version'] != version:
        raise ValueError('compiler identity differs from the qualified installed package')


def verify_native_request(compiler: dict[str, Any], raw: dict[str, bytes], request: dict[str, Any],
                          expected: dict[str, Any]) -> None:
    """Native files must describe the private single-input, emit-only recipe."""
    source = Path(request['path'])
    configuration = json.loads(raw['options'])
    settings = configuration['compilerOptions']
    input_path = Path(compiler['files']['input']['path'])
    workspace = input_path.parent
    commonjs = settings['module'] == 'node16'
    name = source.with_suffix('.cts').name if commonjs and source.suffix == '.ts' else source.name
    if (not workspace.is_absolute() or not workspace.name.startswith('otelc-native-typescript-')
            or workspace.is_relative_to(Path(expected['root'])) or input_path.name != name
            or compiler['files']['options']['path'] != str(workspace / 'tsconfig.json')
            or configuration['files'] != [str(input_path)]):
        raise ValueError('native compiler input/configuration origin differs from the private recipe')
    fixed = {'rootDir': str(workspace), 'outDir': str(workspace / 'emitted'), 'noEmit': False,
             'noEmitOnError': False, 'noCheck': True, 'noResolve': True, 'incremental': False,
             'composite': False, 'types': [], 'sourceMap': True,
             'moduleResolution': 'node16' if commonjs else 'bundler'}
    if settings['module'] not in ('node16', 'preserve') or any(settings.get(key) != value for key, value in fixed.items()):
        raise ValueError('native compiler options differ from the emit-only recipe')
    extension = {'.cts': '.cjs', '.mts': '.mjs'}.get(input_path.suffix, '.js')
    output = workspace / 'emitted' / input_path.with_suffix(extension).name
    if (compiler['files']['output']['path'] != str(output)
            or compiler['files']['map']['path'] != str(output) + '.map'):
        raise ValueError('native compiler output origin differs from its input')


def selected_source(expected: dict[str, Any], name: str) -> bool:
    """Use the authoritative policy selector for the recorded logical source."""
    selection = expected['resolved_plan']['source_matchers']
    return Selection(tuple(selection['include']), tuple(selection['exclude']), paths=True).accepts(name)


def verify_api_request(compiler: dict[str, Any], raw: dict[str, bytes], compiled: dict[str, Any], source: dict[str, Any]) -> None:
    """The API must consume the original string with its recorded execution format."""
    options = json.loads(raw['options'])
    module = 1 if compiled['format'] == 'commonjs' else 200  # Qualified TS 6.0.3 CommonJS/Preserve.
    if (compiler['files']['input']['path'] != source['path'] or options['module'] != module
            or options['sourceMap'] is not True or options['noEmit'] is not False):
        raise ValueError('API compiler input/options differ from its source/format')


def verify_preparation_map(raw: dict[str, bytes], compiler: dict[str, Any], source: dict[str, Any]) -> None:
    """Synthetic native input maps back to this original source, not an equal-byte substitute."""
    preparation = json.loads(raw['preparation_map'])
    contents = preparation['sourcesContent']
    if (preparation['sources'] != [source['path']] or len(contents) != 1
            or preparation['file'] != Path(compiler['files']['input']['path']).name
            or hashlib.sha256(contents[0].encode()).hexdigest() != source['text_sha256']):
        raise ValueError('native preparation map does not bind the original text/origin')


def verify_compilation(runtime: Path, prefix: str, compilation: dict[str, Any], compiled: dict[str, Any],
                       request: dict[str, Any], source: dict[str, Any], expected: dict[str, Any]) -> None:
    """Connect raw compiler consumption/emission to the remapped compiler output."""
    compiler = compilation['compiler']
    verify_compiler_identity(compiler, expected)
    raw = compiler_files(runtime, prefix, compiler)
    plain = expected['plain'] or not selected_source(expected, request['logical_source'])
    profile = 'none' if plain else 'original-identities-v1'
    if compiler['transformer_profile'] != profile:
        raise ValueError('compiler transformer differs from the resolved selection')
    if compiler['backend'] == 'api':
        verify_api_request(compiler, raw, compiled, source)
    else:
        verify_native_request(compiler, raw, request, expected)
        verify_preparation_map(raw, compiler, source)
    if compiler['backend'] == 'api' or plain:
        if hashlib.sha256(raw['input']).hexdigest() != source['text_sha256']:
            raise ValueError('compiler input differs from the original decoded text')
    raw_output = raw['output'].decode()
    stripped = re.sub(r'\n//# sourceMappingURL=.*(?:\n|$)', '\n', raw_output)
    if hashlib.sha256(stripped.encode()).hexdigest() != compiled['sha256']:
        raise ValueError('compiler output does not match the recorded compiled module')


def verify_load_request(request: dict[str, Any]) -> None:
    """A recorded URL must resolve to its own source origin, including query variants."""
    url = urlsplit(request['url'])
    try:
        resolved = str(Path(unquote(url.path)).resolve(strict=True))
    except OSError as error:
        raise ValueError('module request has no readable source origin') from error
    if (url.scheme != 'file' or url.netloc not in ('', 'localhost')
            or resolved != request['path']
            or request['module_type'] not in ('javascript', 'typescript')):
        raise ValueError('module request changed its source URL/origin')


def record_module_phase(item: dict[str, Any], attempts: dict[int, dict[str, Any]],
                        phases: dict[int, dict[str, Any]]) -> None:
    """Require one request and one instance of each phase at its original origin."""
    load_id = item['load_id']
    if item['kind'] == 'module_requested':
        if load_id in attempts:
            raise ValueError('duplicate module load identity')
        verify_load_request(item)
        attempts[load_id] = item
        phases[load_id] = {}
        return
    if load_id not in attempts or item['kind'] in phases[load_id]:
        raise ValueError('module phase has no unique preceding request')
    if item['path'] != attempts[load_id]['path']:
        raise ValueError('module phase changed its source origin')
    if item['kind'] != 'compilation' and any(item[key] != attempts[load_id][key] for key in ('url', 'logical_source')):
        raise ValueError('module phase changed its requested URL/name')
    phases[load_id][item['kind']] = item


def module_phases(records: list[dict[str, Any]]) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Index unique requests and their ordered phases without collapsing equal origins."""
    attempts: dict[int, dict[str, Any]] = {}
    phases: dict[int, dict[str, dict[str, Any]]] = {}
    for item in records:
        if item['kind'] not in ('module_requested', 'compilation', 'compiled', 'instrumented', 'loaded'):
            continue
        load_id = item['load_id']
        if type(load_id) is not int or load_id <= 0:
            raise ValueError('invalid module load identity')
        record_module_phase(item, attempts, phases)
    return attempts, phases


def verify_module_records(runtime: Path, prefix: str, records: list[dict[str, Any]], expected: dict[str, Any]) -> None:
    """Every attempted in-scope load needs its own complete output, even if errors are caught."""
    attempts, phases = module_phases(records)
    sources = {item['path']: item for item in records if item['kind'] == 'source'}
    if not sources.keys() <= {item['path'] for item in attempts.values()}:
        raise ValueError('source input has no recorded module request')
    for load_id, request in attempts.items():
        final = 'loaded' if expected['plain'] or not selected_source(expected, request['logical_source']) else 'instrumented'
        if final not in phases[load_id]:
            raise ValueError('module load did not finish: ' + request['url'])
        if request['module_type'] == 'typescript':
            if not {'compilation', 'compiled'} <= phases[load_id].keys():
                raise ValueError('TypeScript compiler evidence is missing')
            verify_compilation(runtime, prefix, phases[load_id]['compilation'], phases[load_id]['compiled'],
                               request, sources[request['path']], expected)


def loaded_inputs(runtime: Path, prefix: str, expected: dict[str, Any]) -> list[dict[str, Any]]:
    """Reject unknown or truncated records and changed observed inputs."""
    records = []
    sources: set[tuple[str, str]] = set()
    for line in (runtime / (prefix + '-sources.jsonl')).read_text().splitlines():
        record = json.loads(line)
        if not isinstance(record, dict) or record.get('kind') not in (
                'source', 'compiler_config', 'absent_config', 'package_context', 'absent_package', 'startup',
                'module_requested', 'compilation', 'compiled', 'instrumented', 'loaded'):
            raise ValueError('invalid loaded input record')
        if record['kind'] in ('compiled', 'instrumented', 'loaded'):
            verify_emission(runtime, prefix, record, sources)
        elif record['kind'] not in ('module_requested', 'compilation', 'startup'):
            verify_read_input(record)
        if record['kind'] == 'source':
            sources.add((record['path'], record['sha256']))
        records.append(record)
    verify_module_records(runtime, prefix, records, expected)
    node_recipe.verify_startup(records, expected)
    return records


def verify_read_input(record: dict[str, Any]) -> None:
    """Recheck original/configuration bytes or the recorded absence of a file."""
    if record['kind'] in ('absent_config', 'absent_package'):
        if os.path.lexists(record['path']):
            raise ValueError('previously absent input changed: ' + record['path'])
        return
    if str(Path(record['requested_path']).resolve(strict=True)) != record['path']:
        raise ValueError('input resolution changed: ' + record['requested_path'])
    current = identity(Path(record['path']))
    if any(current.get(key) != record[key] for key in ('path', 'sha256', 'size')):
        raise ValueError('loaded source or compiler config changed: ' + record['path'])


def verify(runtime: Path, expected: dict[str, Any]) -> dict[str, Any]:
    """Check retained launch/source evidence after application and SDK shutdown."""
    runtime = runtime.resolve(strict=True)
    if expected['schema_version'] != 3:
        raise ValueError('unsupported Node source evidence schema')
    node_recipe.verify(runtime, expected['request'], expected, producer=Path(__file__).resolve().parent.parent)
    prefix = 'node-plain' if expected['plain'] else 'node'
    filename = runtime / (prefix + '-launch.json')
    if json.loads(filename.read_text()) != expected:
        raise ValueError('launch manifest changed')
    if package_context(Path(expected['entry']['path'])) != expected['entry_context']:
        raise ValueError('entry package context changed')
    for item in [expected['node'], expected['entry'], *expected['generated']]:
        if identity(Path(item['path'])) != item:
            raise ValueError('launch input changed: ' + item['path'])
    verify_tooling(expected)
    records = []
    if not (expected['plain'] and expected['language'] == 'javascript'):
        records = loaded_inputs(runtime, prefix, expected)
        if not any(item['kind'] == 'source' and item['path'] == expected['entry']['path'] for item in records):
            raise ValueError('launch entry is missing from loaded source evidence')
    result = {'schema_version': 3, 'launch_id': expected['launch_id'], 'phase': 'verified',
        'action_delivery_verified': False, 'loaded_inputs': records, 'launch_manifest': identity(filename)}
    if not expected['plain']:
        sdk_report = runtime / 'node-status.json'
        sdk = json.loads(sdk_report.read_text())
        if sdk['launch_id'] != expected['launch_id']:
            raise ValueError('SDK report belongs to another launch')
        startup = records[0]['startup']
        if startup['pid'] != sdk['pid']:
            raise ValueError('Node startup and SDK report identify different applications')
        result.update(application_pid=sdk['pid'], sdk_pid=sdk['sdk_pid'], sdk_report=identity(sdk_report))
    return result


def finish(runtime: Path, expected: dict[str, Any]) -> bool:
    """Retain a failed verification as evidence and never publish it as success."""
    try:
        report = verify(runtime, expected)
    except (ValueError, OSError, KeyError) as error:
        report = {'schema_version': 3, 'launch_id': expected['launch_id'], 'phase': 'failed',
            'action_delivery_verified': False, 'error': str(error)}
    prefix = 'node-plain' if expected['plain'] else 'node'
    descriptor = os.open(runtime / (prefix + '-inputs.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(report, output, sort_keys=True)
        output.write('\n')
    return report['phase'] == 'verified'
