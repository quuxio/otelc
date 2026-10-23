"""Private isolated compiler/pyc reader; target code is never executed."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import resource
import sys
from types import CodeType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from otelc.python_code_descriptor import encode
from otelc.python_input_journal import MAX_PATH, MAX_SOURCE

MAX_HEADER = MAX_PATH * 6 + 4096
MAX_OUTPUT = MAX_SOURCE + MAX_HEADER


def read_code(request: dict, content: bytes) -> tuple[CodeType, dict]:
    """Compile retained bytes or decode one version-bound whole pyc payload."""
    if request['kind'] == 'source':
        code = compile(content, request['filename'], 'exec',
                       flags=request['flags'], dont_inherit=True,
                       optimize=request['optimize'])
        return code, {'compile_flags': request['flags'], 'optimize': request['optimize']}
    if len(content) < 16 or content[:4] != importlib.util.MAGIC_NUMBER:
        raise ValueError('pyc magic/header')
    flags = int.from_bytes(content[4:8], 'little')
    if flags & ~3:
        raise ValueError('pyc flags')
    stream = io.BytesIO(content[16:])
    code = marshal.load(stream)
    if stream.read(1) or type(code) is not CodeType:
        raise ValueError('pyc payload')
    return code, {'pyc_flags': flags, 'pyc_magic': content[:4].hex(),
                  'pyc_validation': 'not-checked', 'pyc_header': content[8:16].hex(),
                  'pyc_mode': ('checked-hash' if flags & 2 else 'unchecked-hash')
                              if flags & 1 else 'timestamp'}


def main() -> int:
    """Refuse malformed input and bound CPU/output without an in-process fallback."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    try:
        header = sys.stdin.buffer.readline(MAX_HEADER + 1)
        if len(header) > MAX_HEADER or not header.endswith(b'\n'):
            raise ValueError('worker request header')
        request = json.loads(header)
        if (type(request) is not dict or set(request) != {
                'kind', 'filename', 'flags', 'optimize', 'python_version'}
                or request['kind'] not in ('source', 'pyc')
                or type(request['filename']) is not str or not request['filename']
                or '\0' in request['filename'] or len(os.fsencode(request['filename'])) > MAX_PATH
                or type(request['flags']) is not int or not 0 <= request['flags'] <= 0x7fffffff
                or type(request['optimize']) is not int or request['optimize'] not in (0, 1, 2)
                or (request['kind'] == 'pyc' and (request['flags'] or request['optimize']))
                or type(request['python_version']) is not int
                or request['python_version'] != sys.hexversion):
            raise ValueError('worker request profile')
        content = sys.stdin.buffer.read(MAX_SOURCE + 1)
        if len(content) > MAX_SOURCE:
            raise ValueError('worker input size')
        code, details = read_code(request, content)
        if type(code) is not CodeType:
            raise ValueError('worker result is not code')
        descriptor = encode(code)
        if len(descriptor) > MAX_SOURCE:
            raise ValueError('worker descriptor size')
        result = {'schema_version': 1, 'python_version': sys.hexversion,
                  'path_hex': os.fsencode(code.co_filename).hex(),
                  'input_sha256': hashlib.sha256(content).hexdigest(),
                  'descriptor_sha256': hashlib.sha256(descriptor).hexdigest(),
                  'descriptor_size': len(descriptor), 'details': details}
        sys.stdout.buffer.write(json.dumps(result).encode('ascii') + b'\n' + descriptor)
        return 0
    except (ValueError, TypeError, SyntaxError, EOFError, OverflowError,
            RecursionError, MemoryError, UnicodeError) as error:
        sys.stderr.write(type(error).__name__ + '\n')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
