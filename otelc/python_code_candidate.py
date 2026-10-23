"""Own a bounded post-run code worker, separate from the application and SDK."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile
from typing import Literal

from otelc.file_inputs import identity
from otelc.python_code_index import Descriptor, decode
from otelc.python_input_journal import CodeInput, MAX_PATH, MAX_SOURCE

MAX_HEADER = MAX_PATH * 6 + 4096
MAX_OUTPUT = MAX_SOURCE + MAX_HEADER
WORKER = Path(__file__).with_name('python_code_worker.py')


@dataclass(frozen=True)
class Candidate:
    """A diagnostic decode is distinct from compilation provenance or coverage."""

    status: Literal['decoded', 'incomplete']
    descriptor: Descriptor | None
    evidence: dict
    reason: str | None = None


def build(interpreter: dict, *, python_version: int, content: bytes,
          filename: str, kind: Literal['source', 'pyc'], flags: int = 0,
          optimize: int = 0, timeout: float = 10) -> Candidate:
    """Process retained bytes with an explicitly identified selected interpreter.

    No source path is reopened. The interpreter identity is rechecked after
    completion, but this does not establish its whole loaded-library closure.
    -I/-S excludes application import paths/site hooks. CPU, output and elapsed
    time are bounded; this is process isolation, not an OS memory sandbox.
    """
    if (type(content) is not bytes or len(content) > MAX_SOURCE
            or type(filename) is not str or not filename or '\0' in filename
            or len(filename.encode('utf-8', 'surrogatepass')) > MAX_PATH
            or kind not in ('source', 'pyc')
            or type(flags) is not int or not 0 <= flags <= 0x7fffffff
            or type(optimize) is not int or optimize not in (0, 1, 2)
            or (kind == 'pyc' and (flags or optimize))
            or type(python_version) is not int or python_version >> 24 != 3
            or type(timeout) not in (int, float) or not 0 < timeout <= 30):
        raise ValueError('invalid isolated code candidate request')
    request = {'kind': kind, 'filename': filename, 'flags': flags,
               'optimize': optimize, 'python_version': python_version}
    evidence = {'input_sha256': hashlib.sha256(content).hexdigest(),
                'request': request, 'interpreter': interpreter, 'source_complete': False}
    try:
        if identity(Path(interpreter['path'])) != interpreter:
            return Candidate('incomplete', None, evidence, 'interpreter-identity')
        worker_before = identity(WORKER)
        encoder = WORKER.with_name('python_code_descriptor.py')
        encoder_before = identity(encoder)
        argv = [interpreter['path'], '-I', '-S', '-B', str(WORKER)]
        evidence.update(argv=argv, worker=worker_before, encoder=encoder_before)
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(argv, input=json.dumps(request).encode('ascii') + b'\n' + content,
                stdout=output, stderr=errors, timeout=timeout, cwd=WORKER.parent,
                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'PYTHONUTF8': '1'}, check=False)
            evidence['returncode'] = result.returncode
            output.seek(0); payload = output.read(MAX_OUTPUT + 1)
            errors.seek(0); evidence['stderr_hex'] = errors.read(4096).hex()
        if (identity(Path(interpreter['path'])) != interpreter
                or identity(WORKER) != worker_before or identity(encoder) != encoder_before):
            return Candidate('incomplete', None, evidence, 'worker-input-changed')
        if result.returncode:
            return Candidate('incomplete', None, evidence, 'worker-refused')
        if len(payload) > MAX_OUTPUT:
            return Candidate('incomplete', None, evidence, 'worker-output-size')
        header, separator, descriptor = payload.partition(b'\n')
        if not separator or len(header) > MAX_HEADER:
            raise ValueError('worker output header')
        metadata = json.loads(header)
        if (type(metadata) is not dict or set(metadata) != {
                'schema_version', 'python_version', 'path_hex', 'input_sha256',
                'descriptor_sha256', 'descriptor_size', 'details'}
                or type(metadata['schema_version']) is not int or metadata['schema_version'] != 1
                or type(metadata['python_version']) is not int or metadata['python_version'] != python_version
                or type(metadata['descriptor_size']) is not int or metadata['descriptor_size'] != len(descriptor)
                or metadata['input_sha256'] != evidence['input_sha256']
                or metadata['descriptor_sha256'] != hashlib.sha256(descriptor).hexdigest()
                or type(metadata['path_hex']) is not str or type(metadata['details']) is not dict):
            raise ValueError('worker output identity')
        value = decode(CodeInput(bytes.fromhex(metadata['path_hex']), descriptor,
                                 1, 0, python_version, 2))
        evidence['result'] = metadata
        return Candidate('decoded', value, evidence)
    except subprocess.TimeoutExpired:
        return Candidate('incomplete', None, evidence, 'worker-timeout')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return Candidate('incomplete', None, evidence, 'worker-failure')
