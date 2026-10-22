"""Own only the official thread-context patches installed for this launch."""

from __future__ import annotations

from opentelemetry.instrumentation.threading import ThreadingInstrumentor


class PythonTaskContext:
    """Native asyncio copies context; the official instrumentor covers threads."""

    def __init__(self, enabled: bool) -> None:
        self._owned: ThreadingInstrumentor | None = None
        if not enabled:
            return
        instrumentor = ThreadingInstrumentor()
        if instrumentor.is_instrumented_by_opentelemetry:
            return
        try:
            instrumentor.instrument()
            if not instrumentor.is_instrumented_by_opentelemetry:
                raise RuntimeError('official Python thread instrumentation did not start')
        except BaseException:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
            raise
        self._owned = instrumentor

    def close(self) -> None:
        """Preserve patches which were already present before our installation."""
        if self._owned is not None:
            instrumentor, self._owned = self._owned, None
            instrumentor.uninstrument()
