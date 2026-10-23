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

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('EntryCapture.c', 'ImportCapture.c', 'InputJournal.c', 'InputJournal.h', 'NativeMain.c')


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


def toolchain() -> tuple[dict, list[str]]:
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
    identity = {'interpreter': digest(Path(sys.executable)), 'version': sys.version,
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
    return identity, flags


def verify(directory: Path, expected: dict) -> Path:
    """Never reuse a truncated or altered product from an existing build namespace."""
    manifest = json.loads((directory / 'build.json').read_text())
    library = 'libotelc_python_inputs' + ('.dylib' if sys.platform == 'darwin' else '.so')
    if (manifest['inputs'] != expected or manifest['library'] != library
            or set(manifest['products']) != {'otelc-python-inputs', library}):
        raise ValueError('native Python build inputs differ')
    for name in ('otelc-python-inputs', library):
        if (directory / name).is_symlink() or digest(directory / name) != manifest['products'][name]:
            raise ValueError('native Python build product changed')
    return directory / 'otelc-python-inputs'


def build(directory: Path | None = None, *, coverage: bool = False) -> Path:
    """Publish a complete tool pair once; running processes keep their original inode."""
    identity, flags = toolchain()
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
        sources = ROOT / 'native/python'
        link = (['-dynamiclib', '-install_name', '@rpath/' + library] if sys.platform == 'darwin'
                else ['-shared', '-fPIC', '-Wl,-soname,' + library])
        origin = '@loader_path' if sys.platform == 'darwin' else '$ORIGIN'
        selected = identity['compiler']['path']
        commands = [[selected, *link, str(sources / 'EntryCapture.c'), str(sources / 'ImportCapture.c'),
                     str(sources / 'InputJournal.c'),
                     '-o', str(candidate / library), *options],
                    [selected, str(sources / 'NativeMain.c'), str(candidate / library),
                     '-Wl,-rpath,' + origin, '-Wl,-rpath,' + str(sysconfig.get_config_var('LIBDIR')),
                     '-o', str(candidate / 'otelc-python-inputs'), *options]]
        for command in commands:
            subprocess.run(command, check=True)
        current, current_flags = toolchain()
        current.update(flags=current_flags, coverage=coverage)
        if current != identity:
            raise ValueError('native Python build inputs changed while compiling')
        products = {name: digest(candidate / name) for name in ('otelc-python-inputs', library)}
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
    print(build(coverage=bool(os.environ.get('OTELC_NATIVE_COVERAGE'))))
