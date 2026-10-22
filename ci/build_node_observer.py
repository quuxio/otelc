"""Build the public-V8 observer against the exact selected Node version and ABI."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def matching_headers(include: Path, version: str, abi: str) -> None:
    """Refuse a binary whose headers merely share a major version with the runtime."""
    header = (include / 'node_version.h').read_text()
    values = [re.search(r'#define NODE_' + key + r'\s+(\d+)', header)
              for key in ('MAJOR_VERSION', 'MINOR_VERSION', 'PATCH_VERSION', 'MODULE_VERSION')]
    if any(value is None for value in values):
        raise ValueError('Node version headers are incomplete')
    major, minor, patch, found_abi = [value.group(1) for value in values if value is not None]
    if '.'.join((major, minor, patch)) != version or found_abi != abi:
        raise ValueError('Node headers must match the exact running version and module ABI')


def build() -> Path:
    """Select installed matching headers; downloading or replacing a toolchain is separate."""
    selected = os.environ.get('OTELC_NODE') or shutil.which('node')
    if not selected:
        raise ValueError('Node 24.11+ is required')
    info = json.loads(subprocess.check_output([selected, '-p',
        'JSON.stringify({version:process.versions.node,abi:process.versions.modules,executable:process.execPath})'], text=True))
    if tuple(map(int, info['version'].split('.'))) < (24, 11, 0):
        raise ValueError('Node 24.11+ is required')
    include = Path(os.environ.get('OTELC_NODE_INCLUDE', str(Path(info['executable']).resolve().parent.parent / 'include/node')))
    matching_headers(include, info['version'], info['abi'])
    compiler = os.environ.get('CXX') or ('/opt/homebrew/opt/llvm@22/bin/clang++' if sys.platform == 'darwin' else 'clang++')
    output = Path('build/tools')
    output.mkdir(parents=True, exist_ok=True)
    library = output / f"otelc_node_observer-{info['version']}.node"
    flags = ['-fprofile-instr-generate', '-fcoverage-mapping'] if os.environ.get('OTELC_NATIVE_COVERAGE') else []
    with tempfile.TemporaryDirectory(prefix='node-observer-', dir=output) as temporary:
        candidate = Path(temporary) / library.name
        command = [compiler, '-std=c++20', '-shared', '-fPIC', '-O2', *flags, '-I' + str(include),
                   'native/node/PromiseObserver.cpp', '-o', str(candidate)]
        if sys.platform == 'darwin':
            command.extend(['-undefined', 'dynamic_lookup'])
        subprocess.run(command, check=True)
        # A running Node process can still map the previous inode. Never truncate
        # its shared library, or publish an incomplete compiler output.
        os.replace(candidate, library)
    print(f"Promise observer: Node {info['version']} ABI {info['abi']}; {library}")
    return library


if __name__ == '__main__':
    build()
