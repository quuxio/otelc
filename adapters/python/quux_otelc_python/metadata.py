"""Read interpreter metadata without dispatching application string hooks."""
from collections.abc import Mapping


def text(value: str) -> str:
    """Copy a possibly subclassed metadata string through the builtin operation."""
    return str.__str__(value)


def member(namespace: Mapping[object, object], name: str, default: object = None) -> object:
    """Read a builtin namespace without hashing or comparing application keys."""
    for key, value in namespace.items():
        if str.__eq__(name, key) is True:
            return value
    return default


def class_members(cls: type) -> dict[str, object] | None:
    """Snapshot an ordinary class; custom keys cannot be resolved passively."""
    namespace = type.__getattribute__(cls, '__dict__').copy()
    # A subclass key can override hash/equality. Matching just its Unicode text
    # could select a hidden entry rather than the method Python actually uses.
    return namespace if all(type(key) is str for key in namespace) else None
