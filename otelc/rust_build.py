"""Retained standalone Rust builds driven by the common instrumentation policy."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from otelc import native_build, native_gate, native_launch, rust_compiler
from otelc.config import Policy
from otelc.file_inputs import checked_file
from otelc.python_sdk_io import decode

ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'rust1981-standalone-sync-v1'
MANIFEST = 'rust-source-build.json'


def validate_policy(policy: Policy) -> None:
    """Keep unsupported lifetime/context requests explicit until their profiles exist."""
    if 'rust' not in policy.data['languages']:
        raise ValueError('Rust build requires an enabled rust language')
    if policy.data['lifetimes']['enabled'] or any(policy.data['context'].values()):
        raise ValueError('Rust automatic lifetimes and task/service context are not implemented yet')


def selection(policy: dict[str, Any]) -> dict[str, Any]:
    """Compiled selectors stay fixed while runtime collection/emission can change."""
    return {key: policy[key] for key in ('sources', 'functions')}


def dependency_paths(raw: str, root: Path) -> list[Path]:
    """Reuse the archive's Rust dep-info escaping, without retaining env! values."""
    joined = raw.replace('\\\n', '')
    first = joined.splitlines()[0] if joined else ''
    _, separator, names = first.partition(': ')
    if not separator:
        raise ValueError('invalid Rust dependency information')
    values, token = [], ''
    index = 0
    while index < len(names):
        char = names[index]
        if char == '\\':
            index += 1
            if index == len(names):
                raise ValueError('unfinished Rust dependency escape')
            token += names[index]
        elif char == '$' and names[index:index + 2] == '$$':
            token += '$'
            index += 1
        elif char.isspace():
            if token:
                values.append(token)
                token = ''
        else:
            token += char
        index += 1
    if token:
        values.append(token)
    paths = sorted({(root / name).resolve(strict=True) for name in values})
    if not 1 <= len(paths) <= 65536 or any(not path.is_relative_to(root) or not path.is_file() for path in paths):
        raise ValueError('Rust dependencies must be bounded files inside the application root')
    return paths


def inventory(path: Path, root: Path, sources: dict[str, str], mode: str) -> dict[str, Any]:
    """Bind compiler-owned source/name/byte ranges before applying common selectors."""
    _, raw = checked_file(path, nofollow=True, include_content=True, max_bytes=8 * 1024 * 1024)
    value = decode(raw)
    fields = {'schema_version', 'kind', 'mode', 'definitions', 'sources', 'source_complete'}
    if (set(value) != fields or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['kind'] != 'otelc.rust.compiler-inventory' or value['mode'] != mode
            or value['sources'] != sources or value['source_complete'] is not False
            or type(value['definitions']) is not list or len(value['definitions']) > 65536):
        raise ValueError('Rust compiler inventory differs from the requested original inputs')
    identities = []
    for row in value['definitions']:
        if (type(row) is not dict or set(row) != {'identity', 'file', 'compiler_name', 'start', 'end', 'asynchronous', 'expanded'}
                or any(type(row[key]) is not str or not row[key] for key in ('identity', 'file', 'compiler_name'))
                or any(type(row[key]) is not bool for key in ('asynchronous', 'expanded'))
                or any(type(row[key]) is not int for key in ('start', 'end'))
                or row['file'] not in sources):
            raise ValueError('invalid Rust compiler definition')
        source = Path(row['file'])
        if (row['identity'] != source.relative_to(root).as_posix() + '::' + row['compiler_name']
                or not 0 <= row['start'] <= row['end'] <= source.stat().st_size):
            raise ValueError('Rust compiler definition identity or byte range differs')
        identities.append(row['identity'])
    if identities != sorted(set(identities)):
        raise ValueError('Rust compiler definitions must be ordered and unique')
    return value


def selected(policy: Policy, root: Path, value: dict[str, Any]) -> list[str]:
    """Use the same include/exclude precedence as every other language adapter."""
    result = []
    for row in value['definitions']:
        source = Path(row['file']).relative_to(root).as_posix()
        if policy.selection('sources').accepts(source) and policy.selection('functions').accepts(row['compiler_name']):
            if (row['asynchronous'] or row['expanded'] or '{closure' in row['compiler_name']
                    or len(row['identity'].encode()) > 4096):
                raise ValueError('selected Rust async, macro, closure or oversized body needs its completion profile')
            result.append(row['identity'])
    return result


@dataclass(frozen=True)
class Build:
    """A caller-held standalone build, not commands selected by a stored manifest."""
    directory: Path
    encoded: str

    def manifest(self) -> dict[str, Any]:
        """Decode a fresh primitive record without sharing mutable aliases."""
        return decode(self.encoded.encode())


def build(policy: Policy, root: Path, entry: Path, output: Path, *,
          tools_directory: Path, sdk_directory: Path, optimisation: int = 2) -> Build:
    """Compile stock and woven standalone crates without editing original sources."""
    validate_policy(policy)
    root = root.resolve(strict=True)
    entry = (root / entry).resolve(strict=True)
    if (not root.is_dir() or root == Path('/') or not entry.is_file() or entry.suffix != '.rs'
            or not entry.is_relative_to(root) or type(optimisation) is not int or optimisation not in (0, 1, 2, 3)):
        raise ValueError('Rust build requires an original .rs entry and optimisation 0..3')
    if (root / 'Cargo.toml').exists():
        raise ValueError('Cargo/build-script integration requires its separate Rust build profile')
    output = output.resolve()
    if rust_compiler.physical_within(output, root):
        raise ValueError('Rust build output must be outside the application root')
    compiler = rust_compiler.read(tools_directory)
    tools = compiler.manifest()
    sdk = native_build.sdk(sdk_directory)
    before = native_gate.identity(entry)
    output.mkdir(mode=0o700)
    env = rust_compiler.environment(Path(tools['sysroot']))
    rustc, driver = tools['tools'][0]['path'], tools['products'][0]['path']
    flags = [str(entry), '--crate-name', 'otelc_app', '--edition=2024', '--sysroot', tools['sysroot'],
             '-Copt-level=' + str(optimisation), '-g']
    deps = output / 'original.d'
    rust_compiler.execute([rustc, *flags, '--emit=dep-info=' + str(deps)], output, 'discover', env, cwd=root)
    _, raw = checked_file(deps, nofollow=True, include_content=True, max_bytes=4 * 1024 * 1024)
    text = raw.decode()
    # Compiler dep-info can contain values from env!(). Keep no such values in
    # retained diagnostics; this initial profile explicitly refuses those inputs.
    environment_inputs = any(line.startswith('# env-dep:') for line in text.splitlines())
    deps.write_text('\n'.join(line for line in text.splitlines() if not line.startswith('# env-dep:')) + '\n')
    if environment_inputs:
        raise ValueError('Rust env! inputs require their separate retained build-context profile')
    inputs = [native_gate.identity(path) for path in dependency_paths(text, root)]
    sources = {row['path']: row['sha256'] for row in inputs if Path(row['path']).suffix == '.rs'}
    if str(entry) not in sources or before != native_gate.identity(entry):
        raise ValueError('Rust original entry changed during input discovery')
    requests = []

    def compile_mode(mode: str, names: list[str]) -> dict[str, Any]:
        request = {'schema_version': 1, 'mode': mode, 'root': str(root), 'entry': str(entry),
                   'sources': sources, 'selected': names, 'output': str(output / (mode + '-inventory.json'))}
        filename = output / (mode + '-request.json')
        filename.write_text(json.dumps(request, sort_keys=True, indent=2) + '\n')
        arguments = [driver, *flags, '-o', str(output / 'instrumented')]
        if mode == 'weave':
            arguments += ['--extern', 'otelc_rust_bridge=' + tools['products'][1]['path'],
                '-L', 'native=' + str(Path(sdk['library']['path']).parent), '-l', 'dylib=otelc_native_runtime',
                '-C', 'link-arg=' + str(output / 'bootstrap.o'),
                '-C', 'link-arg=-Wl,-rpath,' + str(Path(sdk['library']['path']).parent)]
        rust_compiler.execute(arguments, output, mode, env | {'OTELC_RUST_REQUEST': str(filename)}, cwd=root)
        requests.append(native_gate.identity(filename))
        return inventory(Path(request['output']), root, sources, mode)

    inspected = compile_mode('inspect', [])
    names = selected(policy, root, inspected)
    rust_compiler.execute([rustc, *flags, '-o', str(output / 'plain')], output, 'plain', env, cwd=root)
    bootstrap = output / 'bootstrap.cpp'
    bootstrap.write_text(native_launch.BOOTSTRAP)
    rust_compiler.execute([sdk['toolchain']['directory'] + '/clang++', '-std=c++17', '-O2', '-c',
        str(bootstrap), '-I', str(ROOT / 'adapters/native/launcher/include'), '-o', str(output / 'bootstrap.o')],
        output, 'bootstrap', env, cwd=root)
    woven = compile_mode('weave', names)
    if {**inspected, 'mode': 'weave'} != woven:
        raise ValueError('Rust inspection and weaving consumed different declarations')
    if inputs != [native_gate.identity(Path(row['path'])) for row in inputs]:
        raise ValueError('Rust original inputs changed during compilation')
    rust_compiler.verify(compiler)
    if native_build.sdk(sdk_directory) != sdk:
        raise ValueError('Rust SDK inputs changed during compilation')
    value = {'schema_version': 1, 'kind': 'otelc.rust.source-build', 'profile': PROFILE,
        'root': str(root), 'entry': str(entry), 'policy': policy.data,
        'policy_sha256': native_launch.digest(policy.data),
        'selection_sha256': native_launch.digest(selection(policy.data)),
        'tools': native_gate.identity(compiler.directory / rust_compiler.MANIFEST),
        'sdk': native_gate.identity(Path(sdk_directory).resolve() / native_build.SDK_MANIFEST),
        'inputs': inputs, 'requests': requests, 'selected': names, 'optimisation': optimisation,
        'inventories': [native_gate.identity(output / (mode + '-inventory.json')) for mode in ('inspect', 'weave')],
        'products': [native_gate.identity(output / lane) for lane in ('plain', 'instrumented')],
        'source_complete': False}
    encoded = json.dumps(value, sort_keys=True, indent=2) + '\n'
    (output / MANIFEST).write_text(encoded)
    result = Build(output, encoded)
    verify(result, policy)
    return result


def verify(value: Build, policy: Policy) -> dict[str, Any]:
    """Recheck fixed product paths and selectors without invoking a compiler."""
    validate_policy(policy)
    if type(value) is not Build:
        raise ValueError('Rust launch requires a caller-held build record')
    _, raw = checked_file(value.directory / MANIFEST, nofollow=True, include_content=True, max_bytes=16 * 1024 * 1024)
    if raw.decode() != value.encoded:
        raise ValueError('Rust build record changed')
    data = value.manifest()
    fields = {'schema_version', 'kind', 'profile', 'root', 'entry', 'policy', 'policy_sha256', 'selection_sha256',
              'tools', 'sdk', 'inputs', 'requests', 'selected', 'optimisation', 'inventories', 'products', 'source_complete'}
    if (set(data) != fields or type(data['schema_version']) is not int or data['schema_version'] != 1
            or data['kind'] != 'otelc.rust.source-build' or data['profile'] != PROFILE
            or data['source_complete'] is not False or type(data['root']) is not str
            or type(data['entry']) is not str or type(data['policy']) is not dict
            or type(data['optimisation']) is not int or data['optimisation'] not in (0, 1, 2, 3)):
        raise ValueError('Rust build profile or compiled policy selection changed')
    compiled = Policy.parse(data['policy'])
    validate_policy(compiled)
    if (data['policy'] != compiled.data or data['policy_sha256'] != native_launch.digest(compiled.data)
            or data['selection_sha256'] != native_launch.digest(selection(policy.data))
            or selection(compiled.data) != selection(policy.data)):
        raise ValueError('Rust build profile or compiled policy selection changed')
    root = Path(data['root'])
    if root.resolve(strict=True) != root or rust_compiler.physical_within(value.directory, root):
        raise ValueError('Rust original root or external output changed')
    for key, expected_name in (('tools', rust_compiler.MANIFEST), ('sdk', native_build.SDK_MANIFEST)):
        row = data[key]
        if type(row) is not dict or type(row.get('path')) is not str or Path(row['path']).name != expected_name:
            raise ValueError('Rust build tool record is not its fixed kind')
        if native_gate.identity(Path(row['path'])) != row:
            raise ValueError('Rust build tool record changed')
    rust_compiler.read(Path(data['tools']['path']).parent)
    native_build.sdk(Path(data['sdk']['path']).parent)
    if (type(data['inputs']) is not list or not 1 <= len(data['inputs']) <= 65536
            or any(type(row) is not dict or type(row.get('path')) is not str
                   or not Path(row['path']).is_relative_to(root) for row in data['inputs'])
            or len({row['path'] for row in data['inputs']}) != len(data['inputs'])
            or data['entry'] not in {row['path'] for row in data['inputs']}):
        raise ValueError('Rust original input catalogue changed')
    for row in data['inputs']:
        if native_gate.identity(Path(row['path'])) != row:
            raise ValueError('Rust original input changed')
    for key, names in (('requests', ('inspect-request.json', 'weave-request.json')),
                       ('inventories', ('inspect-inventory.json', 'weave-inventory.json')),
                       ('products', ('plain', 'instrumented'))):
        if data[key] != [native_gate.identity(value.directory / name) for name in names]:
            raise ValueError('Rust fixed build product changed: ' + key)
    sources = {row['path']: row['sha256'] for row in data['inputs'] if Path(row['path']).suffix == '.rs'}
    observed = inventory(value.directory / 'inspect-inventory.json', root, sources, 'inspect')
    woven = inventory(value.directory / 'weave-inventory.json', root, sources, 'weave')
    if selected(policy, root, observed) != data['selected'] or {**observed, 'mode': 'weave'} != woven:
        raise ValueError('Rust retained compiler selection changed')
    return data


def read(directory: Path, policy: Policy) -> Build:
    """Load a retained build using current policy, with no build or compiler call."""
    directory = directory.resolve(strict=True)
    _, raw = checked_file(directory / MANIFEST, nofollow=True, include_content=True, max_bytes=16 * 1024 * 1024)
    result = Build(directory, raw.decode())
    verify(result, policy)
    return result
