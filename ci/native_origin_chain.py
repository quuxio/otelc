"""Prepare an explicit native-entry observer map from the selected CPython image.

Only the declared macOS ARM64 source-file path is supported. This metadata is
not an observation or source-origin claim; the native observer must check the
loaded image and adjacent call chain before accepting a returned root.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from importlib.util import MAGIC_NUMBER
from pathlib import Path
import re
import shutil
import struct
import subprocess
from typing import Any

DISABLED = b'#define OTELC_ORIGIN_CHAIN_AVAILABLE 0\n'
MAX_IMAGE = 128 * 1024 * 1024
MAX_SYMBOLS = 16 * 1024 * 1024
MAX_DISASSEMBLY = 1024 * 1024


def disabled(reason: str) -> tuple[bytes, dict[str, Any]]:
    """An absent optional observer must not claim a native mapping."""
    return DISABLED, {'available': False, 'reason': reason,
                      'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def uuid(data: bytes) -> bytes:
    """Read one UUID from a bounded little-endian ARM64 Mach-O image."""
    if not 32 <= len(data) <= MAX_IMAGE:
        raise ValueError('native origin image size is unsupported')
    magic, cpu, subtype, kind, count, size, _, reserved = struct.unpack_from('<8I', data)
    if (magic != 0xfeedfacf or cpu != 0x0100000c or subtype & 0xff
            or kind != 6 or reserved
            or not 0 < count <= 4096 or not 0 < size <= MAX_DISASSEMBLY
            or 32 + size > len(data)):
        raise ValueError('native origin image architecture or commands are unsupported')
    offset, end, found = 32, 32 + size, []
    for _ in range(count):
        if offset + 8 > end:
            raise ValueError('native origin image command is truncated')
        command, width = struct.unpack_from('<II', data, offset)
        if width < 8 or width % 8 or offset + width > end:
            raise ValueError('native origin image command width is invalid')
        if command == 0x1b:
            if width != 24:
                raise ValueError('native origin image UUID width is invalid')
            found.append(data[offset + 8:offset + 24])
        offset += width
    if offset != end or len(found) != 1 or found[0] == bytes(16):
        raise ValueError('native origin image needs exactly one nonempty UUID')
    return found[0]


def chain(symbols: str, disassemble: Callable[[str], str], version: tuple[int, int]) -> list[dict[str, Any]]:
    """Require one direct return site for each declared adjacent native frame."""
    if version not in ((3, 12), (3, 14)) or len(symbols.encode()) > MAX_SYMBOLS:
        raise ValueError('native origin compiler profile or symbol size is unsupported')
    table = sorted((int(address, 16), name) for address, name in
                   re.findall(r'^([0-9a-f]+) [tT] (\S+)$', symbols, re.M))
    entries = {name: (start, table[index + 1][0]) for index, (start, name) in enumerate(table[:-1])}
    simple = '__PyRun_SimpleFileObject' if version == (3, 12) else '__PyRun_SimpleFile'
    any_file = '__PyRun_AnyFileObject' if version == (3, 12) else '__PyRun_AnyFile'
    run_file = '_pyrun_file' if version == (3, 12) else '__PyRun_File'
    names = ['_sys_audit_tstate', '__PySys_Audit', '_run_mod', run_file,
             simple, any_file, '_pymain_run_file_obj', '_pymain_run_file', '_Py_RunMain']
    rows = []
    for index, name in enumerate(names):
        if name not in entries:
            raise ValueError('native origin image is missing a required function')
        start, end = entries[name]
        text = disassemble(name)
        if len(text.encode()) > MAX_DISASSEMBLY or not 0 <= start < end:
            raise ValueError('native origin function bounds are unsupported')
        sites: list[int] = []
        if index:
            sites = [int(address, 16) + 4 for address, target in
                     re.findall(r'^\s*([0-9a-f]+):.*\tbl\s+(\S+)\s*$', text, re.M)
                     if target == names[index - 1]]
            if len(sites) != 1 or not start <= sites[0] < end:
                raise ValueError('native origin adjacent call is missing or ambiguous')
        if name == '_run_mod':
            compiles = re.findall(r'^\s*([0-9a-f]+):.*\tbl\s+__PyAST_Compile\s*$', text, re.M)
            if not compiles or int(compiles[0], 16) >= sites[0]:
                raise ValueError('native origin compiler-return path is unsupported')
        rows.append({'function': name, 'start': start, 'end': end,
                     'return': sites[0] if sites else 0,
                     'disassembly_sha256': hashlib.sha256(text.encode()).hexdigest()})
    return rows


def prepare(library: Path, *, platform: str, machine: str,
            version: tuple[int, int], llvm: Path) -> tuple[bytes, dict[str, Any]]:
    """Keep journal-1 builds usable when the explicit origin profile is absent."""
    own = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if platform != 'darwin' or machine != 'arm64' or version not in ((3, 12), (3, 14)):
        return disabled('unsupported_profile')
    nm, objdump = shutil.which('nm'), llvm / 'llvm-objdump'
    if nm is None or not objdump.is_file():
        return disabled('missing_tools')
    if library.stat().st_size > MAX_IMAGE:
        raise ValueError('native origin image exceeds bound')
    before = library.read_bytes()
    image_hash = hashlib.sha256(before).hexdigest()
    tools = {name: {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
             for name, path in (('nm', Path(nm)), ('objdump', objdump))}
    try:
        image_uuid = uuid(before)
        symbols = subprocess.check_output([nm, str(library)], text=True, timeout=30)
        rows = chain(symbols, lambda name: subprocess.check_output(
            [str(objdump), '--macho', '--disassemble', '--dis-symname', name, str(library)],
            text=True, timeout=30), version)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        if library.read_bytes() != before:
            raise ValueError('native origin image changed during inspection') from error
        return DISABLED, {'available': False, 'reason': 'unsupported_image',
                          'builder_sha256': own, 'library_sha256': image_hash}
    if library.read_bytes() != before:
        raise ValueError('native origin image changed during inspection')
    if any(hashlib.sha256(Path(value['path']).read_bytes()).hexdigest() != value['sha256']
           for value in tools.values()):
        raise ValueError('native origin inspection tool changed')
    path = os.fsencode(library.resolve(strict=True)) + b'\0'
    lines = ['#define OTELC_ORIGIN_CHAIN_AVAILABLE 1',
             '#define OTELC_ORIGIN_MAGIC_PREFIX ' + str(int.from_bytes(MAGIC_NUMBER[:2], 'little')),
             'typedef struct { uintptr_t start, end, pc; } OriginFrame;',
             'static const OriginFrame origin_chain[] = {']
    lines += ['    {0x%x, 0x%x, 0x%x},' % (row['start'], row['end'], row['return']) for row in rows]
    lines += ['};', 'static const unsigned char origin_uuid[16] = {' +
              ','.join(str(value) for value in image_uuid) + '};',
              'static const char origin_library[] = {' + ','.join(str(value) for value in path) + '};']
    header = ('\n'.join(lines) + '\n').encode()
    metadata = {'available': True, 'profile': 'cpython-native-source-entry-v1',
                'magic_number': MAGIC_NUMBER.hex(),
                'builder_sha256': own, 'library_sha256': image_hash, 'uuid': image_uuid.hex(),
                'chain': rows, 'header_sha256': hashlib.sha256(header).hexdigest(),
                'tools': tools}
    return header, metadata
