"""Bind frozen declarations to observed ordinary imports and live SDK ownership."""
from importlib import _bootstrap
from importlib.machinery import SourceFileLoader
from types import CodeType, FrameType, ModuleType
import weakref

from .witness_callsite import bind_site
from .witness_code import CodeBindingError, CodeRegistry, fingerprint
from .witness_entry import EntryAnchor
from .witness_manifest import EDGES, FrozenProgram
from .witness_owner import OwnerProfile
from .witness_sdk import WitnessSDK
from .witness_state import Bindings


_BRIDGE_CODE = _bootstrap._call_with_frames_removed.__code__
_LOADER_CODE = SourceFileLoader.exec_module.__code__
_LOADER_NAMESPACE = next(base.__dict__['__dict__'] for base in SourceFileLoader.__mro__
                         if '__dict__' in base.__dict__)
_MODULE_NAMESPACE = ModuleType.__dict__['__dict__']
_LOADER_MRO = SourceFileLoader.__mro__


def _member(namespace, name: str):
    """Read bounded builtin namespaces without hashing/comparing application keys."""
    for index, (key, value) in enumerate(namespace.items()):
        if index >= 64:
            break
        if type(key) is str and key == name:
            return value
    return None


def _loader_method(name: str):
    for base in _LOADER_MRO:
        value = _member(type.__getattribute__(base, '__dict__'), name)
        if value is not None:
            return value
    return None


_LOADER_METHODS = {name: _loader_method(name) for name in (
    'exec_module', 'get_code', 'get_filename', 'get_data', 'source_to_code', 'path_stats')}


def _qualified_loader() -> bool:
    # Read class namespaces, never bind a newly installed application descriptor.
    return (type.__getattribute__(SourceFileLoader, '__mro__') is _LOADER_MRO
            and all(_loader_method(name) is value for name, value in _LOADER_METHODS.items()))


class WitnessBootstrap:
    """Install before primary entry; never preload providers or fabricate frames."""

    def __init__(self, runtime, program: FrozenProgram, primary: CodeType):
        if type(program) is not FrozenProgram:
            raise CodeBindingError('invalid_frozen_program')
        # Rebuild and compare the frozen declaration before trusting any fields.
        self.program = FrozenProgram.load(program.bytes(), program.root, runtime.plan)
        self.record = self.program.record()
        self.runtime, self.state = runtime, runtime.witness_state
        expected = self.record
        if (self.state is None or self.state.available or self.state.phase != 'idle'
                or self.state.bindings.build_id != expected['build_id']
                or self.state.bindings.selection_id != expected['selection_id']):
            raise CodeBindingError('wrong_frozen_runtime')
        self.registry = CodeRegistry()
        self.codes = {}
        self.modules = set()
        self.sdk = None
        self.closed = False
        self._bind_module(expected['roles']['primary']['path'], primary)
        self.entry = EntryAnchor(self.registry, primary, self._site('envelope'))

    def _code(self, role: str):
        reference = self.codes.get(role)
        return reference() if reference is not None else None

    def _site(self, edge: str):
        caller, callee = EDGES[edge]
        return bind_site(self.registry, self._code(caller), self._code(callee),
                         self.record['sites'][edge]['call_offset'])

    def _bind_module(self, path: str, code: CodeType) -> None:
        if (path in self.modules or type(code) is not CodeType
                or fingerprint(code) != self.record['sources'][path]['module_probe_id']):
            raise CodeBindingError('wrong_compiled_module')
        for role, declaration in self.record['roles'].items():
            if declaration['path'] != path:
                continue
            matches = [code] if role == 'primary' else [value for value in code.co_consts
                       if type(value) is CodeType and value.co_qualname == declaration['function']]
            if len(matches) != 1 or fingerprint(matches[0]) != declaration['probe_id']:
                raise CodeBindingError('wrong_compiled_role')
            self.registry.register(matches[0])
            self.codes[role] = weakref.ref(matches[0])
        self.modules.add(path)

    def _observe_import(self, frame: FrameType) -> None:
        code = frame.f_code
        if type(code.co_name) is not str or code.co_name != '<module>' or type(code.co_filename) is not str:
            return
        path = next((name for name in self.record['sources']
                     if str(self.program.root / name) == code.co_filename), None)
        if path is None:
            return
        caller = frame.f_back
        loader_frame = caller.f_back if caller is not None and caller.f_code is _BRIDGE_CODE else None
        if loader_frame is None or loader_frame.f_code is not _LOADER_CODE:
            return  # Directly executed module copies are not import observations.
        namespace = loader_frame.f_locals
        loader, module = _member(namespace, 'self'), _member(namespace, 'module')
        if (type(loader) is not SourceFileLoader or type(module) is not ModuleType or not _qualified_loader()
                or _member(namespace, 'code') is not code
                or _MODULE_NAMESPACE.__get__(module, ModuleType) is not frame.f_globals):
            raise CodeBindingError('unsupported_source_loader')
        storage = _LOADER_NAMESPACE.__get__(loader, SourceFileLoader)
        if type(storage) is not dict or len(storage) != 2:
            raise CodeBindingError('unsupported_source_loader')
        loader_path, name = _member(storage, 'path'), _member(storage, 'name')
        if type(loader_path) is not str or type(name) is not str or loader_path != code.co_filename:
            raise CodeBindingError('unsupported_source_loader')
        expected_name = path.removesuffix('.py').replace('/', '.').removesuffix('.__init__')
        module_name = _member(frame.f_globals, '__name__')
        if name != expected_name or type(module_name) is not str or module_name != name:
            raise CodeBindingError('wrong_import_name')
        self.program.validate_sources()
        self._bind_module(path, code)

    def watches(self, code: CodeType) -> bool:
        return any(code is self._code(role) for role in self.record['roles'])

    def entered(self, frame: FrameType):
        if self.closed:
            return None
        try:
            if self.sdk is not None:
                # A reload of frozen source remains an invalidation, including
                # after provisional end; it cannot replace a bound compiler role.
                self._observe_import(frame)
                return self.sdk.entered(frame)
            event = self.entry.entered(frame)
            if event is not None and event.kind == 'invalidated':
                self.state.invalidate()
            if frame.f_code is not self._code('primary'):
                self._observe_import(frame)
            if frame.f_code is self._code('rendezvous') and self._site('rendezvous').matches(frame):
                profile = OwnerProfile(self.registry, self._code('primary'),
                                       *(self._site(edge) for edge in EDGES))
                bindings = Bindings(self.record['build_id'], self.record['selection_id'],
                                     *(self.registry.lookup(self._code(role)) for role in ('rendezvous', 'wrapper', 'target')),
                                     object())
                if not self.state.activate(bindings):
                    return None
                self.sdk = WitnessSDK(self.runtime, self.registry, profile)
                self.sdk.owner.adopt_entry(self.entry, frame)
                return self.sdk.entered(frame)
            return event
        except (CodeBindingError, RuntimeError, OSError):
            self.state.invalidate('wrong_binding')
            return None

    def admitted(self, frame: FrameType, event) -> None:
        if self.sdk is not None:
            self.sdk.admitted(frame, event)

    def exited(self, frame: FrameType, outcome: str) -> None:
        if self.sdk is not None:
            self.sdk.exited(frame, outcome)
        else:
            event = self.entry.exited(frame)
            if event is not None and event.kind in ('invalidated', 'envelope_exit', 'primary_exit'):
                self.state.invalidate('incomplete')

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.sdk is not None:
            self.sdk.close()
        elif self.entry.close() is not None:
            self.state.invalidate('incomplete')
        try:
            self.program.validate_sources()
        except (CodeBindingError, OSError):
            self.state.invalidate('wrong_binding')
        self.registry.close()
