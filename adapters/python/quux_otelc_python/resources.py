"""Explicit resource boundaries; retain scalar frame/token identities only."""
import inspect
import types


def bindings(policy: dict) -> dict[str, str]:
    """Validate resolved bindings before starting SDK threads or application code."""
    entries = policy.get('resources', [])
    if policy.get('boundary') != 'resource':
        if entries:
            raise ValueError('resource bindings require the resource boundary')
        return {}
    if not isinstance(entries, list) or not 1 <= len(entries) <= 128:
        raise ValueError('resource lifetimes require 1..128 explicit bindings')
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {'type_name', 'release', 'completion'}:
            raise ValueError('invalid resource binding')
        name, release = entry.get('type_name'), entry.get('release')
        if (not isinstance(name, str) or not name or name != name.strip() or len(name.encode()) > 1024
                or any(value in name for value in '*?[]') or name in result
                or not isinstance(release, str) or not release.isascii() or not release.isidentifier()
                or release in ('__init__', '__new__', '__del__')
                or len(release) > 128 or entry.get('completion', 'normal_return') != 'normal_return'):
            raise ValueError('invalid resource binding')
        result[name] = release
    return result


class Releases:
    def __init__(self, owner, selected: dict[str, str]):
        self.owner = owner
        self.bindings = selected
        self.methods = frozenset(selected.values())
        self.frames = {}
        self.failed = 0

    def qualified(self, name: str, mapping) -> bool:
        method = mapping.get(self.bindings[name])
        return (type(method) is types.FunctionType and method.__code__.co_argcount > 0
                and method.__code__.co_name == self.bindings[name]
                and not method.__code__.co_flags & (inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR))

    def start(self, frame, code):
        """Observe outermost calls per stack, allowing independent concurrent releases."""
        owner = self.owner
        value = owner.receiver(frame, code)
        with owner.runtime.lock:
            item = owner.instances.get(owner.identities.get(id(value)))
            if item is None or item.reference() is not value:
                return
            cls = type(value)
            if type(cls) is not type:
                return
            method = type.__getattribute__(cls, '__dict__').get(self.bindings[item.name])
            if type(method) is not types.FunctionType or method.__code__ is not code:
                return
            token = owner.identities[item.identity]
            ancestor = frame.f_back
            for _ in range(4096):
                if ancestor is None:
                    break
                if self.frames.get(id(ancestor)) == (item.identity, token):
                    return
                ancestor = ancestor.f_back
            else:
                owner.reject('release_depth')
                return
            if len(self.frames) >= owner.runtime.plan['runtime']['max_active_calls']:
                owner.reject('release_capacity')
                return
            self.frames[id(frame)] = (item.identity, token)

    def returned(self, frame, code):
        owner = self.owner
        try:
            with owner.runtime.lock:
                entry = self.frames.pop(id(frame), None)
                if entry is None or owner.closed or owner.runtime.closed:
                    return
                identity, token = entry
                item = owner.instances.get(token)
                if item is None:
                    return  # Another concurrent normal return already completed it.
                value = owner.receiver(frame, code)
                if id(value) != identity or item.reference() is not value:
                    owner.reject('receiver_changed')
                    return
                owner.complete(token)
        except Exception:
            owner.reject('invalid')

    def unwound(self, frame):
        with self.owner.runtime.lock:
            if self.frames.pop(id(frame), None) is not None:
                self.failed += 1

    def shutdown(self):
        with self.owner.runtime.lock:
            if self.frames:
                self.owner.losses['incomplete_release'] += len(self.frames)
            self.frames.clear()
