"""Read class/receiver metadata without dispatching application hooks."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import CodeType, FrameType, FunctionType, ModuleType
from typing import Any

TYPE_DICTIONARY = type.__dict__['__dict__']
TYPE_MRO = type.__dict__['__mro__']
TYPE_QUALNAME = type.__dict__['__qualname__']
MODULE_DICTIONARY = ModuleType.__dict__['__dict__']


def members(cls: type) -> dict[str, Any]:
    """Non-string keys cannot invoke equality during a lookup of our keys."""
    return {key: value for key, value in TYPE_DICTIONARY.__get__(cls).items()
            if type(key) is str}


def hierarchy(cls: type) -> tuple[type, ...]:
    return TYPE_MRO.__get__(cls)


def is_class(value: object) -> bool:
    """Avoid isinstance's possible fallback to an application's __class__."""
    cls = type(value)
    return cls is type or any(base is type for base in hierarchy(cls))


def resolved(cls: type, name: str) -> object | None:
    for base in hierarchy(cls):
        namespace = members(base)
        if name in namespace:
            return namespace[name]
    return None


def function_code(value: object) -> CodeType | None:
    return value.__code__ if type(value) is FunctionType else None


def receiver(frame: FrameType) -> object | None:
    """The first bound positional argument is also valid when named 'this'."""
    code = frame.f_code
    if code.co_argcount:
        return frame.f_locals.get(code.co_varnames[0])
    if code.co_flags & inspect.CO_VARARGS:
        name = code.co_varnames[code.co_kwonlyargcount]
        values = frame.f_locals.get(name)
        if type(values) is tuple and values:
            return values[0]
    return None


def module_filename(name: object) -> str | None:
    """Iterate raw module keys, avoiding foreign keys or module __getattr__."""
    if type(name) is not str:
        return None
    for key, module in dict.items(sys.modules):
        if type(key) is not str or key != name:
            continue
        if not any(base is ModuleType for base in hierarchy(type(module))):
            return None
        values = MODULE_DICTIONARY.__get__(module)
        for field, value in dict.items(values):
            if type(field) is str and field == '__file__':
                return value if type(value) is str else None
        return None
    return None


def source_name(cls: type, root: Path, initializer: CodeType | None
                ) -> tuple[str, str] | None:
    """Use the actual class's module, including inherited/generated init code."""
    namespace = members(cls)
    filename = module_filename(namespace.get('__module__'))
    candidates = [filename, initializer.co_filename if initializer else None]
    for candidate in candidates:
        if type(candidate) is not str or not Path(candidate).is_absolute():
            continue
        path = Path(candidate).resolve()
        if not path.is_relative_to(root) or path.is_relative_to(Path(__file__).resolve().parent):
            continue
        relative = path.relative_to(root).as_posix()
        qualified = TYPE_QUALNAME.__get__(cls)
        if type(qualified) is not str:
            return None
        return relative, relative.removesuffix('.py').replace('/', '.') + '.' + qualified
    return None


def deferred(value: object) -> bool:
    """Do not mistake a returned coroutine/generator/Future for released work."""
    from types import AsyncGeneratorType, CoroutineType, GeneratorType
    if any(type(value) is kind for kind in (AsyncGeneratorType, CoroutineType, GeneratorType)):
        return True
    return any('__await__' in members(base) for base in hierarchy(type(value)))
