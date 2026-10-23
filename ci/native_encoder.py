"""Reify the maintained encoder's trusted code; never process application code."""

from __future__ import annotations

import ast
import dis
import hashlib
import inspect
import os
import types
from pathlib import Path
from typing import Any

MAX_NODES = 4096
MAX_SOURCE = 65536

# The maintained function's keyword defaults are bound to private C instances,
# static builtin types or our own C callables, never application module globals.
DEFAULTS = {
    '_factory': ('make_encoder', 'PyObject_GetAttrString(json, "make_encoder")'),
    '_quote': ('encode_basestring_ascii', 'PyObject_GetAttrString(json, "encode_basestring_ascii")'),
    '_pack': ('pack', 'PyObject_GetAttrString(structure, "pack")'),
    '_type': ('type', 'Py_NewRef((PyObject *)&PyType_Type)'),
    '_hash': ('object.__hash__', 'PyObject_GetAttrString((PyObject *)&PyBaseObject_Type, "__hash__")'),
    '_getattr': ('getattr', 'native_callable(&native_getattr_method, NULL)'),
    '_code_type': ('CodeType', 'Py_NewRef((PyObject *)&PyCode_Type)'),
    '_len': ('len', 'native_callable(&native_len_method, NULL)'),
    '_bool': ('bool', 'Py_NewRef((PyObject *)&PyBool_Type)'),
    '_int': ('int', 'Py_NewRef((PyObject *)&PyLong_Type)'),
    '_float': ('float', 'Py_NewRef((PyObject *)&PyFloat_Type)'),
    '_complex': ('complex', 'Py_NewRef((PyObject *)&PyComplex_Type)'),
    '_str': ('str', 'Py_NewRef((PyObject *)&PyUnicode_Type)'),
    '_bytes': ('bytes', 'Py_NewRef((PyObject *)&PyBytes_Type)'),
    '_tuple': ('tuple', 'Py_NewRef((PyObject *)&PyTuple_Type)'),
    '_frozenset': ('frozenset', 'Py_NewRef((PyObject *)&PyFrozenSet_Type)'),
    '_slice': ('slice', 'Py_NewRef((PyObject *)&PySlice_Type)'),
    '_ellipsis': ('Ellipsis', 'Py_NewRef(Py_Ellipsis)'),
    '_fs_encoding': ('getfilesystemencoding', 'native_callable(&native_constant_method, encoding)'),
    '_fs_errors': ('getfilesystemencodeerrors', 'native_callable(&native_constant_method, errors)'),
    '_value_error': ('ValueError', 'Py_NewRef(PyExc_ValueError)'),
    '_type_error': ('TypeError', 'Py_NewRef(PyExc_TypeError)'),
}


def load(source: bytes) -> types.CodeType:
    """Build only our bounded helper in the controlled tooling interpreter."""
    if len(source) > MAX_SOURCE:
        raise ValueError('native encoder source exceeds bound')
    module = compile(source, '<otelc.native.encoder>', 'exec', dont_inherit=True, optimize=0)
    definitions = [value for value in ast.parse(source).body
                   if isinstance(value, ast.FunctionDef) and value.name == 'encode']
    functions = [value for value in module.co_consts
                 if type(value) is types.CodeType and value.co_name == 'encode']
    asynchronous = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
    if (len(definitions) != 1 or len(functions) != 1 or functions[0].co_freevars
            or functions[0].co_argcount != 1 or functions[0].co_flags & asynchronous):
        raise ValueError('native encoder must be one closed function')
    return functions[0]


class Factory:
    """Emit matched-CPython constructors for this helper's immutable constants."""

    def __init__(self) -> None:
        self.seen: dict[int, tuple[Any, int]] = {}
        self.pending: set[int] = set()
        self.rows: list[str] = []

    def node(self, value: Any) -> int:
        key = id(value)
        if key in self.pending:
            raise ValueError('native encoder constants must be acyclic')
        if key in self.seen:
            return self.seen[key][1]
        if len(self.seen) >= MAX_NODES:
            raise ValueError('native encoder constant count exceeds bound')
        identity = len(self.seen)
        self.seen[key] = value, identity
        self.pending.add(key)
        expression = self.expression(value, identity)
        if expression is not None:
            self.rows.extend((f'    nodes[{identity}] = {expression};',
                              f'    if (!nodes[{identity}]) goto failed;'))
        self.pending.remove(key)
        return identity

    @staticmethod
    def data(value: bytes) -> str:
        return '(const char[]){' + ','.join(str(byte) for byte in value or b'\0') + '}'

    def expression(self, value: Any, identity: int) -> str | None:
        kind = type(value)
        if value is None:
            return 'Py_NewRef(Py_None)'
        if kind is bool:
            return 'Py_NewRef(Py_True)' if value else 'Py_NewRef(Py_False)'
        if kind is int and -(2 ** 63) < value < 2 ** 63:
            return f'PyLong_FromLongLong({value}LL)'
        if kind is str:
            data = value.encode('utf8', 'surrogatepass')
            return f'PyUnicode_DecodeUTF8({self.data(data)}, {len(data)}, "surrogatepass")'
        if kind is bytes:
            return f'PyBytes_FromStringAndSize({self.data(value)}, {len(value)})'
        if kind is tuple:
            children = [self.node(item) for item in value]
            self.rows.extend((f'    nodes[{identity}] = PyTuple_New({len(value)});',
                              f'    if (!nodes[{identity}]) goto failed;'))
            self.rows.extend(f'    PyTuple_SET_ITEM(nodes[{identity}], {index}, Py_NewRef(nodes[{child}]));'
                             for index, child in enumerate(children))
            return None
        if kind is types.CodeType:
            if any(instruction.opname in ('LOAD_GLOBAL', 'LOAD_NAME', 'IMPORT_NAME')
                   for instruction in dis.get_instructions(value)):
                raise ValueError('native encoder cannot resolve runtime globals or imports')
            values = [self.node(getattr(value, name)) for name in (
                'co_code', 'co_consts', 'co_names', 'co_varnames', 'co_freevars',
                'co_cellvars', 'co_filename', 'co_name', 'co_qualname')]
            tail = [self.node(value.co_linetable), self.node(value.co_exceptiontable)]
            numbers = [value.co_argcount, value.co_posonlyargcount, value.co_kwonlyargcount,
                       value.co_nlocals, value.co_stacksize, value.co_flags]
            arguments = [*(str(number) for number in numbers),
                         *(f'nodes[{index}]' for index in values), str(value.co_firstlineno),
                         *(f'nodes[{index}]' for index in tail)]
            return '(PyObject *)PyUnstable_Code_NewWithPosOnlyArgs(' + ', '.join(arguments) + ')'
        raise ValueError('unsupported trusted encoder constant: ' + kind.__name__)


def header(source: bytes) -> bytes:
    """Emit deterministic C and release every build-only helper reference."""
    function = load(source)
    factory = Factory()
    root = factory.node(function)
    count = len(factory.seen)
    syntax = ast.parse(source)
    definition = next(value for value in syntax.body if isinstance(value, ast.FunctionDef) and value.name == 'encode')
    defaults = {argument.arg: ast.unparse(value) if value is not None else None
                for argument, value in zip(definition.args.kwonlyargs, definition.args.kw_defaults, strict=True)}
    if defaults != {name: expression for name, (expression, _) in DEFAULTS.items()}:
        raise ValueError('maintained native encoder defaults changed')
    lines = ['/* Generated from the maintained encoder, for the exact selected CPython. */',
             f'#define OTELC_ENCODER_SOURCE_SHA256 "{hashlib.sha256(source).hexdigest()}"',
             f'#define OTELC_ENCODER_DEFAULT_COUNT {len(DEFAULTS)}',
             'static PyObject *encoder_code_factory(void) {',
             f'    PyObject *nodes[{count}] = {{0}};', *factory.rows,
             f'    PyObject *result = Py_NewRef(nodes[{root}]);',
             f'    for (size_t index = 0; index < {count}; ++index) Py_XDECREF(nodes[index]);',
             '    return result;', 'failed:',
             f'    for (size_t index = 0; index < {count}; ++index) Py_XDECREF(nodes[index]);',
             '    return NULL;', '}',
             'static int encoder_defaults_factory(PyObject *function, PyObject *json,',
             '                                    PyObject *structure, PyObject *encoding, PyObject *errors) {',
             '    PyObject *defaults = PyDict_New(), *value = NULL;',
             '    if (!defaults) return -1;']
    for name, (_, expression) in DEFAULTS.items():
        lines.extend((f'    value = {expression};',
                      f'    if (!value || PyDict_SetItemString(defaults, "{name}", value)) goto failed;',
                      '    Py_CLEAR(value);'))
    lines.extend(('    int result = PyFunction_SetKwDefaults(function, defaults);',
                  '    Py_DECREF(defaults);', '    return result;', 'failed:',
                  '    Py_XDECREF(value);', '    Py_DECREF(defaults);', '    return -1;', '}'))
    return ('\n'.join(lines) + '\n').encode('ascii')


def generate(root: Path) -> tuple[bytes, dict[str, Any]]:
    """Pin the helper, generator, generated constructors and actual C libraries."""
    import _json
    import _struct

    source = root / 'otelc/python_code_descriptor.py'
    data = source.read_bytes()
    result: dict[str, Any] = {'source_sha256': hashlib.sha256(data).hexdigest(),
              'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    generated = header(data)
    for name, module in (('json', _json), ('struct', _struct)):
        path = Path(module.__file__).resolve(strict=True)
        result[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        generated += (f'#define OTELC_ENCODER_{name.upper()}_PATH ' +
                      Factory.data(os.fsencode(path) + b'\0') + '\n').encode('ascii')
    result['header_sha256'] = hashlib.sha256(generated).hexdigest()
    return generated, result


def inputs(root: Path) -> dict[str, Any]:
    """Return the exact generated-code/dependency identity used by the builder."""
    return generate(root)[1]
