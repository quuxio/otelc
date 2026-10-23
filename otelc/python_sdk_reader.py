"""Observe each owned SDK collection through the public MetricReader base."""

from __future__ import annotations

import threading
import time
from typing import Any

from opentelemetry.sdk.metrics.export import (
    MetricReader, MetricExportResult, MetricsData,
)
from otelc.python_metrics import GatedMetricExporter


class AccountedMetricReader(MetricReader):
    """Keep SDK aggregation/export outcomes visible during periodic collection.

    The stock periodic reader absorbs export failures and its public collect
    method is final. This small scheduler uses the SDK's public collection
    method and declared _receive_metrics extension point, retaining the actual
    result for every owned collection instead of wrapping a private SDK hook.
    """

    def __init__(self, exporter: GatedMetricExporter, *, interval_ms: int,
                 timeout_ms: int) -> None:
        super().__init__()
        self._exporter = exporter
        self._interval = interval_ms / 1000
        self._timeout_ms = timeout_ms
        self._lock = threading.RLock()
        self._collect_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = self._succeeded = self._failed = self._pending = 0
        self._closed = False
        self._shutdown_done = threading.Event()
        self._shutdown_failed = False

    def start(self) -> None:
        """Start after the official provider has registered this reader."""
        with self._lock:
            if self._closed:
                raise RuntimeError('Python SDK metric reader is closed')
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._ticker, name='otelc-python-metrics', daemon=True,
                )
                self._thread.start()

    def snapshot(self) -> dict[str, int | bool]:
        """Report collection attempts independently from HTTP API outcomes."""
        with self._lock:
            return {'started': self._started, 'succeeded': self._succeeded,
                    'failed': self._failed, 'pending': self._pending,
                    'closed': self._closed,
                    'shutdown_complete': self._shutdown_done.is_set(),
                    'shutdown_failed': self._shutdown_failed}

    def _ticker(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._collect_once(self._timeout_ms)
            except Exception:
                # Counts retain the failure; a later collection can recover.
                # SDK diagnostic objects never enter application output.
                continue

    def _collect_once(self, timeout_millis: float) -> None:
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        with self._lock:
            self._started += 1
            self._pending += 1
        acquired = False
        succeeded = False
        try:
            acquired = self._collect_lock.acquire(
                timeout=max(0, deadline - time.monotonic()),
            )
            if not acquired:
                raise TimeoutError('Python SDK collection is still pending')
            remaining = (deadline - time.monotonic()) * 1000
            if remaining <= 0:
                raise TimeoutError('Python SDK collection deadline elapsed')
            super().collect(timeout_millis=remaining)
            if time.monotonic() > deadline:
                raise TimeoutError('Python SDK collection deadline elapsed')
            succeeded = True
        finally:
            if acquired:
                self._collect_lock.release()
            with self._lock:
                self._pending -= 1
                self._succeeded += int(succeeded)
                self._failed += int(not succeeded)

    def _receive_metrics(self, metrics_data: MetricsData,
                         timeout_millis: float = 10_000,
                         **kwargs: Any) -> None:
        if self._exporter.export(
            metrics_data, timeout_millis=timeout_millis,
        ) is not MetricExportResult.SUCCESS:
            raise RuntimeError('Python SDK metric export failed')

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        """A false exporter result must not become an acknowledged flush."""
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        with self._lock:
            if self._closed:
                raise RuntimeError('Python SDK metric reader is closed')
        self._collect_once(timeout_millis)
        remaining = (deadline - time.monotonic()) * 1000
        if remaining <= 0 or not self._exporter.force_flush(remaining):
            raise RuntimeError('Python SDK metric exporter flush failed')
        return True

    def shutdown(self, timeout_millis: float = 30_000,
                 **kwargs: Any) -> None:
        """Stop the scheduler, perform its last collection and close transport."""
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        with self._lock:
            first = not self._closed
            self._closed = True
            self._stop.set()
            thread = self._thread
        if not first:
            if not self._shutdown_done.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError('Python SDK metric reader shutdown is pending')
            if self._shutdown_failed:
                raise RuntimeError('Python SDK metric reader shutdown failed')
            return
        try:
            if thread is not None:
                thread.join(max(0, deadline - time.monotonic()))
                if thread.is_alive():
                    raise TimeoutError('Python SDK periodic collection is pending')
            try:
                self._collect_once(max(0, deadline - time.monotonic()) * 1000)
            finally:
                self._exporter.shutdown(
                    timeout_millis=max(0, deadline - time.monotonic()) * 1000,
                )
        except Exception:
            with self._lock:
                self._shutdown_failed = True
            raise
        finally:
            self._shutdown_done.set()
