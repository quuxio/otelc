"""Build the candidate native entry observer for this exact CPython toolchain."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from ci import native_encoder, native_origin_chain

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('CodeCapture.c', 'EntryCapture.c', 'ImportCapture.c', 'InputJournal.c', 'InputJournal.h',
           'NativeMain.c', 'NativeEncoder.c', 'NativeEncoder.h',
           'FileIOCapture.c', 'FileIOCapture.h', 'OriginPins.c', 'OriginPins.h',
           'OriginChain.c', 'OriginEntry.c', 'OriginEntry.h')


def digest(path: Path) -> dict[str, str]:
    """Retain exact external/tool/source content identities without reading secrets."""
    resolved = path.resolve(strict=True)
    return {'path': str(path), 'resolved_path': str(resolved),
            'sha256': hashlib.sha256(resolved.read_bytes()).hexdigest()}


def compiler() -> str:
    """Use the same installed LLVM family as the existing native observer gate."""
    selected = os.environ.get('CC')
    homebrew = Path('/opt/homebrew/opt/llvm@22/bin/clang')
    selected = selected or (str(homebrew) if sys.platform == 'darwin' and homebrew.is_file() else shutil.which('clang'))
    if not selected:
        raise ValueError('Clang is required for native Python input evidence')
    resolved = shutil.which(selected)
    if resolved is None:
        raise ValueError('CC must identify one installed compiler executable')
    return resolved


def initial_configuration() -> dict[str, dict[str, str]]:
    """Pin the frontend's pre-site spelling without running startup hooks.

    A framework launcher can resolve an alias before sitecustomize changes
    sys.executable again. A venv path must keep its original spelling instead.
    """
    value = json.loads(subprocess.check_output([sys.executable, '-I', '-S', '-B', '-c',
        'import json,sys;print(json.dumps({"executable":sys.executable,"base_executable":sys._base_executable}))'],
        text=True, timeout=10))
    if type(value) is not dict or set(value) != {'executable', 'base_executable'}:
        raise ValueError('native Python initial executable probe is invalid')
    for name, spelling in value.items():
        if type(spelling) is not str or not spelling or '\0' in spelling or not Path(spelling).is_absolute():
            raise ValueError('native Python initial executable probe is invalid')
        if name == 'executable' and not Path(spelling).samefile(sys.executable):
            raise ValueError('native Python initial executable differs from the selected interpreter')
    return {name: digest(Path(spelling)) for name, spelling in value.items()}


def toolchain(*, origin_entry: bool = False) -> tuple[dict, list[str]]:
    """Refuse mismatched headers and free-threaded/unsupported interpreter profiles."""
    if (sys.implementation.name != 'cpython' or sys.version_info < (3, 12)
            or sys.platform not in ('darwin', 'linux')
            or sysconfig.get_config_var('Py_GIL_DISABLED')
            or not (sysconfig.get_config_var('Py_ENABLE_SHARED') or sysconfig.get_config_var('PYTHONFRAMEWORK'))):
        raise ValueError('native Python input evidence requires shared/framework GIL CPython3.12+ on macOS/Linux')
    # CPython's normal and isolated startups can spell the same installation
    # through different prefix symlinks. These are metadata reads, not flags
    # passed to the compiler: bind their one canonical header tree.
    include = Path(sysconfig.get_path('include')).resolve(strict=True)
    version = re.search(r'^#define PY_VERSION\s+"([^"]+)"', (include / 'patchlevel.h').read_text(), re.M)
    if version is None or version[1] != sys.version.split()[0]:
        raise ValueError('native Python headers must match the exact interpreter version')
    config = Path(sysconfig.get_config_var('BINDIR')) / ('python' + sysconfig.get_python_version() + '-config')
    flags = shlex.split(subprocess.check_output([str(config), '--embed', '--cflags', '--ldflags'], text=True))
    library_root = sysconfig.get_config_var('PYTHONFRAMEWORKPREFIX') or sysconfig.get_config_var('LIBDIR')
    python_library = Path(library_root) / sysconfig.get_config_var('LDLIBRARY')
    # Relocatable distributions can omit -L from python-config even though
    # libpython is outside the compiler's default search path. Select the
    # same owned library that the build identity pins below.
    flags.insert(0, '-L' + str(python_library.parent))
    identity = {'interpreter': digest(Path(sys.executable)), 'version': sys.version,
                'initial_configuration': initial_configuration(),
                'cache_tag': sys.implementation.cache_tag, 'config': digest(config),
                'platform': sysconfig.get_platform(), 'machine': platform.machine(),
                'builder': digest(Path(__file__)),
                'environment': {name: os.environ.get(name) for name in (
                    'SDKROOT', 'MACOSX_DEPLOYMENT_TARGET', 'CPATH', 'C_INCLUDE_PATH',
                    'LIBRARY_PATH', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH')},
                'python_library': digest(python_library),
                'headers': [digest(path) for path in sorted(include.rglob('*.h'))],
                'compiler': digest(Path(compiler())),
                'sources': [digest(ROOT / 'native/python' / name) for name in SOURCES]}
    identity['encoder'] = native_encoder.inputs(ROOT)
    origin_header, origin = native_origin_chain.prepare(python_library.resolve(strict=True),
        platform=sys.platform, machine=platform.machine(), version=sys.version_info[:2],
        llvm=Path(compiler()).parent) if origin_entry else native_origin_chain.disabled('not_requested')
    identity['origin_chain'] = {**origin, 'header_sha256': hashlib.sha256(origin_header).hexdigest()}
    return identity, flags


def verify(directory: Path, expected: dict) -> Path:
    """Never reuse a truncated or altered product from an existing build namespace."""
    manifest = json.loads((directory / 'build.json').read_text())
    library = 'libotelc_python_inputs' + ('.dylib' if sys.platform == 'darwin' else '.so')
    if (manifest['inputs'] != expected or manifest['library'] != library
            or set(manifest['products']) != {'otelc-python-inputs', library, 'EncoderFactory.h', 'OriginChain.h'}
            or manifest['products']['EncoderFactory.h']['sha256'] != expected['encoder']['header_sha256']
            or manifest['products']['OriginChain.h']['sha256'] != expected['origin_chain']['header_sha256']):
        raise ValueError('native Python build inputs differ')
    for name in ('otelc-python-inputs', library, 'EncoderFactory.h', 'OriginChain.h'):
        if (directory / name).is_symlink() or digest(directory / name) != manifest['products'][name]:
            raise ValueError('native Python build product changed')
    return directory / 'otelc-python-inputs'


def build(directory: Path | None = None, *, coverage: bool = False, origin_entry: bool = False) -> Path:
    """Publish a complete tool pair once; running processes keep their original inode."""
    identity, flags = toolchain(origin_entry=origin_entry)
    identity.update(flags=flags, coverage=coverage)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    parent = (directory or ROOT / 'build/tools/python-inputs').resolve()
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / key
    if destination.exists():
        return verify(destination, identity)
    library = 'libotelc_python_inputs' + ('.dylib' if sys.platform == 'darwin' else '.so')
    instrumentation = ['-fprofile-instr-generate', '-fcoverage-mapping'] if coverage else []
    options = [*flags, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', *instrumentation]
    with tempfile.TemporaryDirectory(prefix='python-inputs-', dir=parent) as temporary:
        candidate = Path(temporary)
        generated, encoder_inputs = native_encoder.generate(ROOT)
        if encoder_inputs != identity['encoder']:
            raise ValueError('native encoder inputs changed while preparing')
        (candidate / 'EncoderFactory.h').write_bytes(generated)
        origin_header, origin = native_origin_chain.prepare(
            Path(identity['python_library']['resolved_path']), platform=sys.platform,
            machine=platform.machine(), version=sys.version_info[:2], llvm=Path(identity['compiler']['path']).parent
            ) if origin_entry else native_origin_chain.disabled('not_requested')
        origin['header_sha256'] = hashlib.sha256(origin_header).hexdigest()
        if origin != identity['origin_chain']:
            raise ValueError('native origin inputs changed while preparing')
        (candidate / 'OriginChain.h').write_bytes(origin_header)
        sources = ROOT / 'native/python'
        link = (['-dynamiclib', '-install_name', '@rpath/' + library] if sys.platform == 'darwin'
                else ['-shared', '-fPIC', '-Wl,-soname,' + library])
        origin = '@loader_path' if sys.platform == 'darwin' else '$ORIGIN'
        selected = identity['compiler']['path']
        executable_defines = [
            '-DOTELC_PY_SELECTED_EXECUTABLE=' + native_encoder.Factory.data(os.fsencode(identity['interpreter']['path']) + b'\0'),
            '-DOTELC_PY_INITIAL_EXECUTABLE=' + native_encoder.Factory.data(os.fsencode(identity['initial_configuration']['executable']['path']) + b'\0'),
            '-DOTELC_PY_BASE_EXECUTABLE=' + native_encoder.Factory.data(os.fsencode(identity['initial_configuration']['base_executable']['path']) + b'\0')]
        commands = [[selected, *link, str(sources / 'CodeCapture.c'),
                     str(sources / 'EntryCapture.c'), str(sources / 'ImportCapture.c'),
                     str(sources / 'InputJournal.c'), str(sources / 'NativeEncoder.c'),
                     str(sources / 'FileIOCapture.c'), str(sources / 'OriginPins.c'),
                     str(sources / 'OriginChain.c'), str(sources / 'OriginEntry.c'),
                     '-I', str(candidate),
                     '-o', str(candidate / library), *options],
                    [selected, str(sources / 'NativeMain.c'), str(candidate / library),
                     *executable_defines,
                     '-Wl,-rpath,' + origin, '-Wl,-rpath,' + str(sysconfig.get_config_var('LIBDIR')),
                     '-o', str(candidate / 'otelc-python-inputs'), *options]]
        for command in commands:
            subprocess.run(command, check=True)
        current, current_flags = toolchain(origin_entry=origin_entry)
        current.update(flags=current_flags, coverage=coverage)
        if current != identity:
            raise ValueError('native Python build inputs changed while compiling')
        products = {name: digest(candidate / name) for name in ('otelc-python-inputs', library, 'EncoderFactory.h', 'OriginChain.h')}
        for name, value in products.items():
            value.update(path=str(destination / name), resolved_path=str(destination / name))
        (candidate / 'build.json').write_text(json.dumps({'inputs': identity, 'library': library,
            'commands': commands, 'products': products}, indent=2) + '\n')
        try:
            candidate.rename(destination)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
    return verify(destination, identity)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin-entry', action='store_true', help='build the explicit native source-entry observer')
    options = parser.parse_args()
    print(build(coverage=bool(os.environ.get('OTELC_NATIVE_COVERAGE')), origin_entry=options.origin_entry))
