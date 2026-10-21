"""Refuse observed continuation creation inside the private synchronous action."""
import _thread
import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import inspect
import threading
from types import BuiltinMethodType, CodeType, FrameType, FunctionType, MethodType

from .witness_code import CodeBindingError


_FLAGS = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
# Capture the actual qualified standard-library functions before application entry.
_HANDOFFS = tuple(function.__code__ for function in (
    threading.Thread.start, ThreadPoolExecutor.submit,
    asyncio.BaseEventLoop.call_soon, asyncio.BaseEventLoop.call_soon_threadsafe,
    asyncio.BaseEventLoop.call_later, asyncio.BaseEventLoop.call_at,
    asyncio.BaseEventLoop.create_task, asyncio.BaseEventLoop.run_in_executor,
) if type(function) is FunctionType)
_START_THREADS = tuple(value for value in (
    _thread.start_new_thread, _thread.start_new,
    getattr(_thread, 'start_joinable_thread', None),
) if value is not None)
_FUTURES = (asyncio.Future, asyncio.Task)
_FUTURE_CALLBACK = asyncio.Future.add_done_callback
_BUILTIN_OWNER = BuiltinMethodType.__dict__['__self__']
_BUILTIN_NAME = BuiltinMethodType.__dict__['__name__']


def unsupported_callable(value: object) -> bool:
    """Inspect exact builtin metadata without invoking application descriptors."""
    for _ in range(8):
        if type(value) is functools.partial:
            value = value.func
            continue
        if type(value) is MethodType:
            value = value.__func__
        if type(value) is FunctionType:
            code = value.__code__
            return bool(code.co_flags & _FLAGS) or any(code is known for known in _HANDOFFS)
        if issubclass(type(value), BuiltinMethodType):
            # CPython's builtin_method is a specialised builtin-function subtype.
            # Read its builtin descriptors, never an application override.
            owner = _BUILTIN_OWNER.__get__(value, type(value))
            name = _BUILTIN_NAME.__get__(value, type(value))
            if (any(type(owner) is known for known in _FUTURES)
                    and type(name) is str and name == 'add_done_callback'):
                return True
        return value is _FUTURE_CALLBACK or any(value is known for known in _START_THREADS)
    return True  # An over-deep deferred callable cannot qualify this narrow profile.


class WitnessContinuations:
    """Do not patch scheduling APIs, retain payloads or execute application code.

    This is refusal evidence for qualified Python and standard scheduling calls,
    not a general detector for native extensions or arbitrary callback registries.
    """

    def __init__(self, witness):
        self.witness = witness
        self.state = witness.state

    def _scoped(self, frame: FrameType) -> bool:
        if self.state.fault is not None:
            return False
        try:
            return self.witness.contains(frame)
        except CodeBindingError:
            self.state.invalidate('incomplete')
            return False

    def called(self, frame: FrameType, function: object) -> None:
        if self._scoped(frame) and unsupported_callable(function):
            self.state.invalidate('unsupported_profile')

    def execution(self, frame: FrameType, code: CodeType) -> None:
        if code.co_flags & _FLAGS and self._scoped(frame):
            self.state.invalidate('unsupported_profile')
