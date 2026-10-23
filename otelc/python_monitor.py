"""Observe selected original CPython functions without replacing their code."""

from __future__ import annotations

import inspect
import site
import sys
import threading
import weakref
from importlib.machinery import BuiltinImporter
from pathlib import Path
from types import CodeType, FrameType, ModuleType
from typing import Protocol

from otelc.config import Policy
from otelc.python_lifetimes import Lifetimes
from otelc.python_code_descriptor import encode as encode_code

MAX_FUNCTIONS = 4096
MAX_REJECTED_CODES = 4096


class ObservationRuntime(Protocol):
    """The monitoring mechanism owns no exporter, application frame or object."""

    def enter(self, key: int, name: str, *, code_id: int | None = None) -> None: ...
    def leave(self, key: int, error: BaseException | None) -> None: ...
    def suspend(self, key: int) -> None: ...
    def resume(self, key: int) -> None: ...
    def reject(self, reason: str) -> None: ...
    def abandon(self, key: int) -> None: ...


class PythonMonitor:
    """Keep original function/frame/exception identity, including async suspension."""

    def __init__(self, policy: Policy, root: Path, runtime: ObservationRuntime, *,
                 lifetimes: Lifetimes | None = None) -> None:
        if not hasattr(sys, 'monitoring'):
            raise ValueError('Python function monitoring requires CPython 3.12+')
        self.root = root.resolve(strict=True)
        self.runtime = runtime
        self.lifetimes = lifetimes
        self.sources = policy.selection('sources')
        self.functions = policy.selection('functions')
        self._producer = Path(__file__).resolve().parent
        self._names: dict[int, tuple[weakref.ReferenceType[CodeType], str]] = {}
        self._rejected: dict[int, weakref.ReferenceType[CodeType]] = {}
        self._rejected_lock = threading.RLock()
        self.tool: int | None = None
        self._callbacks: list[int] = []
        self._startup_module: ModuleType | None = None
        self._code_observer = None

    def retire_startup_module(self, module: ModuleType) -> None:
        """Remove only our temporary module after CPython's outer import ends."""
        self._startup_module = module

    def selected(self, code: CodeType) -> str | None:
        """Weak identity checks prevent ID reuse without retaining code constants."""
        entry = self._names.get(id(code))
        if entry is not None and entry[0]() is code:
            return entry[1]
        rejected = self._rejected.get(id(code))
        if rejected is not None and rejected() is code:
            return None
        name = self._selection_name(code)
        if name is None:
            self._cache_rejection(code)
            return None
        if len(self._names) >= MAX_FUNCTIONS:
            # No weakref callback can wait on a lifetime/GC lock. Dead slots
            # remain bounded and are reclaimed only when capacity is needed.
            for identity, cached in tuple(self._names.items()):
                if cached[0]() is None and self._names.get(identity) is cached:
                    self._names.pop(identity, None)
        if len(name.encode()) > 1024 or len(self._names) >= MAX_FUNCTIONS:
            self.runtime.reject('function_capacity')
            return None
        self._names[id(code)] = (weakref.ref(code), name)
        return name

    def _cache_rejection(self, code: CodeType) -> None:
        """Bound hot-path exclusions without retaining arbitrary code constants."""
        # GC can enter monitoring while holding the lifetime queue lock.
        # Cache maintenance is optional and must never wait for another owner.
        if not self._rejected_lock.acquire(blocking=False):
            return
        try:
            identity = id(code)
            if identity in self._rejected or len(self._rejected) < MAX_REJECTED_CODES:
                self._rejected[identity] = weakref.ref(code,
                    lambda reference: self._forget_rejection(identity, reference))
        finally:
            self._rejected_lock.release()

    def _forget_rejection(self, identity: int, reference: weakref.ReferenceType[CodeType]) -> None:
        if not self._rejected_lock.acquire(blocking=False):
            return
        try:
            if self._rejected.get(identity) is reference:
                self._rejected.pop(identity, None)
        finally:
            self._rejected_lock.release()

    def _selection_name(self, code: CodeType) -> str | None:
        """Resolve immutable code metadata against this process's fixed policy."""
        if not code.co_flags & inspect.CO_NEWLOCALS:
            return None
        source = Path(code.co_filename)
        if not source.is_absolute():
            return None
        source = source.resolve()
        if not source.is_relative_to(self.root) or source.is_relative_to(self._producer):
            return None
        relative = source.relative_to(self.root).as_posix()
        if not self.sources.accepts(relative):
            return None
        name = relative.removesuffix('.py').replace('/', '.') + '.' + code.co_qualname
        if not self.functions.accepts(name):
            return None
        return name

    def _frame(self) -> FrameType | None:
        """Respect unavailable frame access without changing application results."""
        try:
            return sys._getframe(2)
        except Exception:
            self.runtime.reject('monitor_frame')
            return None

    def _failed(self, reason: str, key: int | None) -> None:
        # Never repeat an introspection operation that has already failed.
        if key is not None:
            try:
                self.runtime.abandon(key)
            except Exception:
                self.runtime.reject('monitor_cleanup')
        self.runtime.reject(reason)

    def _start(self, code: CodeType, offset: int) -> None:
        key = None
        try:
            name = self.selected(code)
            if name is not None:
                frame = self._frame()
                if frame is None:
                    return
                key = id(frame)
                if self._code_observer is None:
                    self.runtime.enter(key, name)
                else:
                    identity = self._code_observer(code, offset)
                    if type(identity) is not int or identity <= 0:
                        self.runtime.reject('source_code')
                        self.runtime.enter(key, name)
                    else:
                        self.runtime.enter(key, name, code_id=identity)
        except Exception:
            self._failed('monitor_start', key)
        if self.lifetimes is not None:
            try:
                if (frame := self._frame()) is not None:
                    self.lifetimes.start(frame)
            except Exception:
                self.lifetimes.reject('monitor_start')

    def _return(self, code: CodeType, offset: int, value: object) -> None:
        frame = self._frame()
        if frame is None:
            return
        if (self._startup_module is not None and code is site.execsitecustomize.__code__
                and frame.f_back is not None and frame.f_back.f_code is site.main.__code__):
            if sys.modules.get('sitecustomize') is self._startup_module:
                del sys.modules['sitecustomize']
            self._startup_module = None
        self._complete(frame, None, value)

    def _unwind(self, code: CodeType, offset: int, error: BaseException) -> None:
        if (frame := self._frame()) is not None:
            self._complete(frame, error)

    def _complete(self, frame: FrameType, error: BaseException | None,
                  value: object = None) -> None:
        if self.lifetimes is not None:
            try:
                self.lifetimes.finish(frame, value, error)
            except Exception:
                self.lifetimes.reject('monitor_completion')
        key = None
        try:
            if (cached := self._names.get(id(frame.f_code))) is not None and cached[0]() is frame.f_code:
                key = id(frame)
                self.runtime.leave(key, error)
        except Exception:
            self._failed('monitor_completion', key)

    def _yield(self, code: CodeType, offset: int, value: object) -> None:
        if (frame := self._frame()) is not None:
            self._suspend(frame, resume=False)

    def _resume(self, code: CodeType, offset: int, *unused: object) -> None:
        if (frame := self._frame()) is not None:
            self._suspend(frame, resume=True)

    def _suspend(self, frame: FrameType, *, resume: bool) -> None:
        key = None
        try:
            if (cached := self._names.get(id(frame.f_code))) is not None and cached[0]() is frame.f_code:
                operation = self.runtime.resume if resume else self.runtime.suspend
                key = id(frame)
                operation(key)
        except Exception:
            self._failed('monitor_context', key)

    def _call(self, code: CodeType, offset: int, callable: object, argument: object) -> None:
        try:
            if (frame := self._frame()) is not None:
                self.lifetimes.class_call(frame, offset, callable)
        except Exception:
            self.lifetimes.reject('monitor_call')

    def _c_return(self, code: CodeType, offset: int, callable: object, argument: object) -> None:
        if (frame := self._frame()) is not None:
            self._class_finished(frame, offset, failed=False)

    def _c_raise(self, code: CodeType, offset: int, callable: object, argument: object) -> None:
        if (frame := self._frame()) is not None:
            self._class_finished(frame, offset, failed=True)

    def _class_finished(self, frame: FrameType, offset: int, *, failed: bool) -> None:
        try:
            self.lifetimes.class_finished(frame, offset, failed=failed)
        except Exception:
            self.lifetimes.reject('monitor_call_completion')

    def install(self) -> None:
        """Use an unoccupied ID, and roll back a partial callback installation."""
        if self.tool is not None:
            raise RuntimeError('Python monitor is already installed')
        # Only the native driver's registered builtin is eligible. Never import
        # an application file with this private name from the search path.
        spec = BuiltinImporter.find_spec('_otelc_inputs')
        if spec is not None:
            module = BuiltinImporter.create_module(spec)
            BuiltinImporter.exec_module(module)
            if module.bind(self._start, encode_code) is not True:
                raise ValueError('native Python code callback binding failed')
            self._code_observer = module.observe
        monitoring = sys.monitoring
        for tool in (3, 4, 2, 5):
            if monitoring.get_tool(tool) is None:
                monitoring.use_tool_id(tool, 'quux.otelc')
                self.tool = tool
                break
        if self.tool is None:
            raise ValueError('no free CPython monitoring tool ID')
        events = monitoring.events
        try:
            callbacks = ((events.PY_START, self._start), (events.PY_RETURN, self._return),
                (events.PY_UNWIND, self._unwind), (events.PY_YIELD, self._yield),
                (events.PY_RESUME, self._resume), (events.PY_THROW, self._resume))
            if self.lifetimes is not None:
                callbacks += ((events.CALL, self._call), (events.C_RETURN, self._c_return),
                              (events.C_RAISE, self._c_raise))
            mask = 0
            for event, callback in callbacks:
                monitoring.register_callback(self.tool, event, callback)
                self._callbacks.append(event)
                mask |= event
            monitoring.set_events(self.tool, mask)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Disable callbacks explicitly before releasing the monitoring ID."""
        if self._rejected_lock.acquire(blocking=False):
            try:
                self._rejected.clear()
            finally:
                self._rejected_lock.release()
        if self.tool is None:
            return
        monitoring = sys.monitoring
        monitoring.set_events(self.tool, 0)
        for event in self._callbacks:
            monitoring.register_callback(self.tool, event, None)
        monitoring.free_tool_id(self.tool)
        self.tool = None
        self._callbacks.clear()
