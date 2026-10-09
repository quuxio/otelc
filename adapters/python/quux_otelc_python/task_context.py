"""Private asyncio and thread-pool contexts, scoped to the external launch adapter."""
import asyncio
import contextvars
import functools
import sys

from .traces import SUPPRESSED
from .worker_context import WorkerContext


class TaskContext:
    def __init__(self, runtime):
        self.runtime = runtime
        self.current = contextvars.ContextVar("quux.otelc.parent", default=None)
        self.previous = {}
        self.original = None
        self.wrapper = None
        self.workers = WorkerContext(self)

    def boundary(self, code):
        return (self.wrapper is not None and code is self.wrapper.__code__
                or code is self.workers.boundary)

    def enter(self, key, identity):
        self.previous[key] = self.current.get()
        self.current.set(identity)

    def suspend(self, key):
        if key in self.previous:
            self.current.set(self.previous.pop(key))

    def resume(self, key):
        with self.runtime.lock:
            frame = self.runtime.pending.get(key)
            if frame is not None and key not in self.previous:
                self.enter(key, frame.trace)

    def release(self, lease):
        with self.runtime.lock:
            self.runtime.traces.release(lease)

    def capture(self, frame):
        monitor = self.runtime.monitor
        key = monitor.parent_key(frame) if monitor is not None else None
        with self.runtime.lock:
            if key is None:
                return self.current.get()
            parent = self.runtime.pending.get(key)
            return parent.trace if parent is not None else SUPPRESSED

    def install(self):
        self.original = asyncio.BaseEventLoop.create_task
        original = self.original
        if getattr(original, "_otelc_task_context", False):
            raise ValueError("asyncio task context hook is already installed")

        @functools.wraps(original)
        def create_task(loop, coroutine, *args, **kwargs):
            context = kwargs.get("context")
            parent = self.capture(sys._getframe()) if context is None else (
                context.get(self.current) if isinstance(context, contextvars.Context) else None)
            with self.runtime.lock:
                lease = self.runtime.traces.acquire(parent) if not self.runtime.closed else None
            token = self.current.set(parent) if context is None else None
            try:
                task = original(loop, coroutine, *args, **kwargs)
            except BaseException:
                self.release(lease)
                raise
            finally:
                if token is not None:
                    self.current.reset(token)
            if lease is not None:
                try:
                    task.add_done_callback(lambda _: self.release(lease), context=contextvars.Context())
                except Exception:
                    # A custom factory may return an incompatible future. Keep its
                    # result intact, but never publish this tree as complete.
                    with self.runtime.lock:
                        self.runtime.traces.reject(parent, "context_hook")
                    self.release(lease)
            return task

        create_task._otelc_task_context = True
        self.wrapper = create_task
        asyncio.BaseEventLoop.create_task = create_task
        try:
            self.workers.install()
        except BaseException:
            asyncio.BaseEventLoop.create_task = original
            raise

    def close(self):
        self.workers.close()
        if asyncio.BaseEventLoop.create_task is self.wrapper:
            asyncio.BaseEventLoop.create_task = self.original
        self.previous.clear()
        self.current.set(None)
