"""Private asyncio contexts, scoped to the external launch adapter."""
import asyncio
import contextvars
import functools


class TaskContext:
    def __init__(self, runtime):
        self.runtime = runtime
        self.current = contextvars.ContextVar("quux.otelc.parent", default=None)
        self.previous = {}
        self.original = None
        self.wrapper = None

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

    def install(self):
        self.original = asyncio.BaseEventLoop.create_task
        original = self.original
        if getattr(original, "_otelc_task_context", False):
            raise ValueError("asyncio task context hook is already installed")

        @functools.wraps(original)
        def create_task(loop, coroutine, *args, **kwargs):
            context = kwargs.get("context")
            parent = self.current.get() if context is None else (
                context.get(self.current) if isinstance(context, contextvars.Context) else None)
            with self.runtime.lock:
                lease = self.runtime.traces.acquire(parent) if not self.runtime.closed else None
            try:
                task = original(loop, coroutine, *args, **kwargs)
            except BaseException:
                self.release(lease)
                raise
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

    def close(self):
        if asyncio.BaseEventLoop.create_task is self.wrapper:
            asyncio.BaseEventLoop.create_task = self.original
        self.previous.clear()
        self.current.set(None)
