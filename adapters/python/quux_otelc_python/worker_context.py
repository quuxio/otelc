"""Qualified ThreadPoolExecutor handoffs using only private trace identities."""
import concurrent.futures
import functools
import sys


class WorkerContext:
    def __init__(self, context):
        self.context = context
        self.runtime = context.runtime
        self.original = None
        self.wrapper = None
        self.boundary = None

    def install(self):
        original = concurrent.futures.ThreadPoolExecutor.submit
        if getattr(original, "_otelc_worker_context", False):
            raise ValueError("thread pool context hook is already installed")
        self.original = original

        @functools.wraps(original)
        def submit(executor, function, /, *args, **kwargs):
            parent = self.context.capture(sys._getframe())
            if parent is None:
                return original(executor, function, *args, **kwargs)
            with self.runtime.lock:
                lease = self.runtime.traces.acquire(parent) if not self.runtime.closed else None

            def work():
                # The executor's original queue already owns the callable and
                # arguments. Neither this runtime nor its completion callback
                # holds the Future, callable, payload or application frames.
                token = self.context.current.set(parent)
                try:
                    return function(*args, **kwargs)
                finally:
                    self.context.current.reset(token)

            self.boundary = work.__code__
            try:
                future = original(executor, work)
            except BaseException:
                self.context.release(lease)
                raise
            if lease is not None:
                try:
                    future.add_done_callback(lambda _: self.context.release(lease))
                except Exception:
                    with self.runtime.lock:
                        self.runtime.traces.reject(parent, "context_hook")
                    self.context.release(lease)
            return future

        submit._otelc_worker_context = True
        self.wrapper = submit
        concurrent.futures.ThreadPoolExecutor.submit = submit

    def close(self):
        if concurrent.futures.ThreadPoolExecutor.submit is self.wrapper:
            concurrent.futures.ThreadPoolExecutor.submit = self.original
