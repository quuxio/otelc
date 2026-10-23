"""Observe selected original CPython functions without replacing their code."""

from __future__ import annotations

import inspect
import site
import sys
from pathlib import Path
from types import CodeType, FrameType, ModuleType
from typing import Protocol

from otelc.config import Policy

MAX_FUNCTIONS = 4096


class ObservationRuntime(Protocol):
    """The monitoring mechanism owns no exporter, application frame or object."""

    def enter(self, key: int, name: str) -> None: ...
    def leave(self, key: int, error: BaseException | None) -> None: ...
    def suspend(self, key: int) -> None: ...
    def resume(self, key: int) -> None: ...
    def reject(self, reason: str) -> None: ...
    def abandon(self, key: int) -> None: ...


class PythonMonitor:
    """Keep original function/frame/exception identity, including async suspension."""

    def __init__(self, policy: Policy, root: Path, runtime: ObservationRuntime) -> None:
        if not hasattr(sys, 'monitoring'):
            raise ValueError('Python function monitoring requires CPython 3.12+')
        self.root = root.resolve(strict=True)
        self.runtime = runtime
        self.sources = policy.selection('sources')
        self.functions = policy.selection('functions')
        self._producer = Path(__file__).resolve().parent
        self._names: dict[int, tuple[CodeType, str | None]] = {}
        self.tool: int | None = None
        self._callbacks: list[int] = []
        self._startup_module: ModuleType | None = None

    def retire_startup_module(self, module: ModuleType) -> None:
        """Remove only our temporary module after CPython's outer import ends."""
        self._startup_module = module

    def selected(self, code: CodeType) -> str | None:
        """Identity cache retains code objects so an ID cannot silently be reused."""
        entry = self._names.get(id(code))
        if entry is not None:
            return entry[1]
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
        if len(name.encode()) > 1024 or len(self._names) >= MAX_FUNCTIONS:
            self.runtime.reject('function_capacity')
            return None
        self._names[id(code)] = (code, name)
        return name

    def _start(self, code: CodeType, offset: int) -> None:
        try:
            name = self.selected(code)
            if name is not None:
                self.runtime.enter(id(sys._getframe(1)), name)
        except Exception:
            self.runtime.abandon(id(sys._getframe(1)))
            self.runtime.reject('monitor_start')

    def _return(self, code: CodeType, offset: int, value: object) -> None:
        frame = sys._getframe(1)
        if (self._startup_module is not None and code is site.execsitecustomize.__code__
                and frame.f_back is not None and frame.f_back.f_code is site.main.__code__):
            if sys.modules.get('sitecustomize') is self._startup_module:
                del sys.modules['sitecustomize']
            self._startup_module = None
        self._complete(frame, None)

    def _unwind(self, code: CodeType, offset: int, error: BaseException) -> None:
        self._complete(sys._getframe(1), error)

    def _complete(self, frame: FrameType, error: BaseException | None) -> None:
        try:
            if id(frame.f_code) in self._names:
                self.runtime.leave(id(frame), error)
        except Exception:
            self.runtime.abandon(id(frame))
            self.runtime.reject('monitor_completion')

    def _yield(self, code: CodeType, offset: int, value: object) -> None:
        self._suspend(sys._getframe(1), resume=False)

    def _resume(self, code: CodeType, offset: int, *unused: object) -> None:
        self._suspend(sys._getframe(1), resume=True)

    def _suspend(self, frame: FrameType, *, resume: bool) -> None:
        try:
            if id(frame.f_code) in self._names:
                operation = self.runtime.resume if resume else self.runtime.suspend
                operation(id(frame))
        except Exception:
            self.runtime.abandon(id(frame))
            self.runtime.reject('monitor_context')

    def install(self) -> None:
        """Use an unoccupied ID, and roll back a partial callback installation."""
        if self.tool is not None:
            raise RuntimeError('Python monitor is already installed')
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
        if self.tool is None:
            return
        monitoring = sys.monitoring
        monitoring.set_events(self.tool, 0)
        for event in self._callbacks:
            monitoring.register_callback(self.tool, event, None)
        monitoring.free_tool_id(self.tool)
        self.tool = None
        self._callbacks.clear()
