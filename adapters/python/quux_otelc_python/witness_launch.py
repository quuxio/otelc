"""Prepare frozen original code and private witness controls for the real launcher."""
import json
import os
from pathlib import Path

from .witness_code import CodeBindingError
from .witness_manifest import FrozenProgram, MAX_MANIFEST_BYTES
from .witness_wire import _pairs, _number


def read_bounded(path: str) -> bytes:
    """Refuse special files and bound the actual manifest/declaration read."""
    from .witness_manifest import read_source
    return read_source(Path(path), MAX_MANIFEST_BYTES)


def freeze(plan: dict, declaration: str) -> None:
    """Write one fresh manifest; do not import or execute application modules."""
    try:
        value = json.loads(read_bounded(declaration), object_pairs_hook=_pairs,
                           parse_float=_number, parse_constant=_number)
        if (type(value) is not dict or set(value) != {'version', 'primary', 'roles', 'call_offsets'}
                or type(value['version']) is not int or value['version'] != 1):
            raise CodeBindingError('invalid_manifest_declaration')
        program = FrozenProgram.freeze(Path.cwd(), plan, value['primary'], value['roles'], value['call_offsets'])
        target = Path(plan['witness']['manifest'])
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(program.bytes())
    except (KeyError, TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise CodeBindingError('invalid_witness_declaration') from error


def prepare(plan: dict, argument: str):
    """Bind one qualified source script spelling to the actual compiler object."""
    program = FrozenProgram.load(read_bounded(plan['witness']['manifest']), Path.cwd(), plan)
    primary = program.record()['roles']['primary']['path']
    filename = argument if os.path.isabs(argument) else os.path.join(os.getcwd(), argument)
    expected = str(program.root / primary)
    if filename != expected:
        raise CodeBindingError('witness_requires_frozen_primary_script_path')
    from .witness_manifest import read_source, MAX_SOURCE_BYTES
    code = compile(read_source(Path(filename), MAX_SOURCE_BYTES), filename, 'exec', dont_inherit=True)
    return program, code
