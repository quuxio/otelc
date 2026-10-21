"""Bounded application identities, SDK metrics and owner-only live admission control."""
import json
import math
import os
import random
import socket
import stat
import threading
import time
import urllib.parse
from email.utils import parsedate_to_datetime
from pathlib import Path
from dataclasses import dataclass
import requests

from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceResponse
from .deadlines import Adapter, CURRENT, Deadline, workers


class Session(requests.Session):
    """Reject invalid/partial OTLP success and keep response bodies out of SDK logs."""
    ack_type = ExportMetricsServiceResponse
    rejected_field = "rejected_data_points"

    def __init__(self):
        super().__init__()
        workers()
        self.mount("http://", Adapter())
        self.mount("https://", Adapter())

    def total_deadline(self, timeout: float) -> float:
        """Use one elapsed budget for all attempts, not a fresh socket idle wait."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise requests.Timeout("invalid OTLP deadline")
        return time.monotonic() + timeout

    def request(self, *args, **kwargs):
        kwargs.update(allow_redirects=False, stream=True)
        timeout = kwargs.get("timeout")
        state = Deadline(self.total_deadline(float(10 if timeout is None else timeout)))
        supervisor, _ = workers()
        supervisor.add(state)
        token = CURRENT.set(state)
        try:
            for attempt in range(6):
                kwargs["timeout"] = state.remaining()
                try:
                    response = super().request(*args, **kwargs)
                except requests.RequestException:
                    if attempt == 5:
                        raise requests.RequestException("OTLP request failed") from None
                    # One immediate stale-connection retry, then bounded backoff.
                    if attempt:
                        self.retry_delay(state, 2 ** (attempt - 1) * random.uniform(0.8, 1.2))
                    continue
                if response.status_code in (429, 502, 503, 504):
                    response.close()
                    if attempt < 5:
                        delay = self.retry_after(response.headers.get("Retry-After"), 2 ** attempt * random.uniform(0.8, 1.2))
                        if math.isfinite(delay) and 0 <= delay < state.remaining():
                            time.sleep(delay)
                            continue
                    # The SDK must not start another wall-clock-based retry loop.
                    response.status_code = 422
                return self.acknowledge(response, state)
            raise requests.RequestException("OTLP request failed")
        finally:
            state.finish()
            supervisor.remove(state)
            CURRENT.reset(token)

    @staticmethod
    def retry_delay(state: Deadline, delay: float) -> None:
        """Retry connection failures only while the original budget permits it."""
        if delay >= state.remaining():
            raise requests.Timeout("OTLP retry deadline elapsed")
        time.sleep(delay)

    @staticmethod
    def retry_after(value: str | None, fallback: float) -> float:
        """Interpret Retry-After without letting a wall-clock date extend the budget."""
        if value is None:
            return fallback
        try:
            value = value.strip()
            return float(value) if value.isdecimal() else max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return fallback

    def acknowledge(self, response: requests.Response, state: Deadline) -> requests.Response:
        """Validate a bounded acknowledgement and redact its content on every path."""
        try:
            if response.status_code == 200:
                body = response.raw.read(65537, decode_content=True)
                state.remaining()
                result = self.ack_type.FromString(body)
                if len(body) > 65536 or getattr(result.partial_success, self.rejected_field):
                    raise ValueError("invalid or partially rejected OTLP batch")
                state.remaining()
            elif 200 <= response.status_code < 400:
                raise ValueError("OTLP requires HTTP 200 acknowledgement")
        except Exception:
            response.status_code = 422
        finally:
            response.close()
            response._content = b""
            response.reason = "OTLP acknowledgement" if response.status_code == 200 else "OTLP export rejected"
        return response



class MetricsSession(Session):
    """Send resolved metric headers without the SDK's generic-header fallback."""

    def __init__(self, headers: dict[str, str], runtime=None):
        super().__init__()
        self.runtime = runtime
        self.signal_headers = dict(headers)
        value = (os.environ.get("OTEL_EXPORTER_OTLP_METRICS_COMPRESSION")
                 or os.environ.get("OTEL_EXPORTER_OTLP_COMPRESSION") or "none")
        try:
            self.compression = Compression(value.strip().lower())
        except ValueError:
            raise ValueError("unsupported OTLP metrics compression") from None

    def total_deadline(self, timeout: float) -> float:
        # The SDK's timeout argument is derived from wall time. Own the entire
        # configured budget here so clock corrections cannot extend or erase it.
        if self.runtime is not None:
            timeout = self.runtime.timeout_ms / 1000
        deadline = super().total_deadline(timeout)
        if self.runtime is not None and self.runtime.shutdown_deadline is not None:
            deadline = min(deadline, self.runtime.shutdown_deadline)
        return deadline

    def request(self, *args, **kwargs):
        self.headers.clear()
        headers = {"Content-Type": "application/x-protobuf"}
        if self.compression != Compression.NoCompression:
            headers["Content-Encoding"] = self.compression.value
        kwargs["headers"] = {**headers, **self.signal_headers}
        try:
            return super().request(*args, **kwargs)
        except (requests.RequestException, UnicodeError, ValueError):
            raise requests.RequestException("OTLP metrics request failed") from None


def export_headers(signal="OTEL_EXPORTER_OTLP_METRICS_HEADERS") -> dict[str, str]:
    value = os.environ.get(signal, os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))
    if len(value.encode()) > 8192:
        raise ValueError("OTLP headers exceed 8192 bytes")
    result = {}
    for item in value.split(","):
        if not item.strip():
            continue
        key, separator, header = item.strip().partition("=")
        if not separator:
            raise ValueError("invalid OTLP headers")
        key, header = urllib.parse.unquote(key), urllib.parse.unquote(header)
        if not key or any(c in key + header for c in "\r\n"):
            raise ValueError("invalid OTLP headers")
        result[key] = header
    return result


class Exporter(MetricExporter):
    def __init__(self, runtime, delegate):
        super().__init__()
        self.runtime, self.delegate = runtime, delegate
        self.signature = None

    def export(self, metrics_data, timeout_millis=10000, **kwargs):
        runtime = self.runtime
        signature = tuple((metric.name, tuple(
            (tuple(sorted(point.attributes.items())), getattr(point, "value", getattr(point, "count", 0)))
            for point in metric.data.data_points))
            for resource in metrics_data.resource_metrics
            for scope in resource.scope_metrics for metric in scope.metrics)
        if signature == self.signature:
            if runtime.traces is not None:
                runtime.traces.flush()
            return MetricExportResult.SUCCESS
        try:
            result = self.delegate.export(metrics_data, timeout_millis=min(timeout_millis, runtime.timeout_ms))
        except Exception:
            result = MetricExportResult.FAILURE
        if result != MetricExportResult.SUCCESS:
            runtime.export_loss += 1
        else:
            self.signature = signature
        if runtime.traces is not None:
            runtime.traces.flush()
        return result

    def force_flush(self, timeout_millis=10000):
        return self.delegate.force_flush(timeout_millis=timeout_millis)

    def shutdown(self, timeout_millis=30000, **kwargs):
        self.delegate.shutdown(timeout_millis=timeout_millis)
        if self.runtime.traces is not None:
            self.runtime.traces.close()


@dataclass
class Frame:
    name: str
    started: int
    metrics: bool
    trace: object = None


class Runtime:
    def __init__(self, plan: dict, delegate=None, trace_delegate=None):
        self.http = None
        self.closed = False
        self.monitor = None
        self.traces = None
        self.lifetimes = None
        self.shutdown_deadline = None
        self.report = None
        self.plan = plan
        if plan.get('lifetimes', {}).get('enabled', False):
            if (plan['lifetimes'].get('boundary') not in ('collection', 'resource') or not plan.get('traces', {}).get('enabled')
                    or type(plan['runtime'].get('max_live_lifetimes')) is not int
                    or not 1 <= plan['runtime']['max_live_lifetimes'] <= 65536):
                raise ValueError('Python lifetimes require tracing, a supported boundary and a valid live-instance limit')
            from .resources import bindings
            bindings(plan['lifetimes'])
        self.enabled = plan["metrics"]["enabled"]
        self.timeout_ms = min(plan["export"]["timeout_ms"], plan["runtime"]["shutdown_timeout_ms"])
        self.calls = {}
        self.pending = {}
        self.loss = {"function_capacity": 0, "active_call_capacity": 0, "incomplete": 0, "invalid": 0}
        self.export_loss = 0
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.socket = None
        self.control = None
        self.socket_identity = None
        identity = {"service.name": plan["resource"]["service_name"], "service.version": plan["resource"]["service_version"], "service.instance.id": str(os.getpid())}
        identity.update(plan["resource"]["attributes"])
        resource = Resource(identity)
        if plan.get("traces", {}).get("enabled", False):
            from .traces import TraceStore
            self.traces = TraceStore(self, resource, trace_delegate)
        if delegate is None:
            session = MetricsSession(export_headers(), self)
            delegate = OTLPMetricExporter(endpoint=plan["metrics_endpoint"], headers=session.signal_headers,
                                          compression=session.compression, timeout=self.timeout_ms / 1000, session=session)
        self.exporter = Exporter(self, delegate)
        self.reader = PeriodicExportingMetricReader(self.exporter, export_interval_millis=plan["export"]["interval_ms"], export_timeout_millis=self.timeout_ms + (self.traces.timeout_ms if self.traces else 0))
        self.provider = MeterProvider(resource=resource, metric_readers=[self.reader], shutdown_on_exit=False, views=[View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(plan["metrics"]["histogram_boundaries_seconds"])) for name in ('otelc.function.duration', 'otelc.lifetime.duration')])
        meter = self.provider.get_meter("quux.otelc", "0.1.0")
        self.counter = meter.create_counter("otelc.function.calls", unit="{call}")
        self.unwinds = meter.create_counter("otelc.function.unwinds", unit="{observation}")
        self.duration = meter.create_histogram("otelc.function.duration", unit="s")
        if plan.get('lifetimes', {}).get('enabled', False):
            from .lifetimes import Lifetimes
            self.lifetimes = Lifetimes(self, meter)
        meter.create_observable_counter("otelc.runtime.dropped_observations", callbacks=[self.loss_points], unit="{observation}")
        meter.create_observable_counter("otelc.export.dropped_batches", callbacks=[lambda _: [Observation(self.export_loss)]], unit="{batch}")
        if self.traces is not None:
            meter.create_observable_counter("otelc.trace.dropped_trees", callbacks=[self.trace_loss_points], unit="{trace}")
        try:
            if plan["runtime"]["control_socket"]:
                self.bind_control(plan["runtime"]["control_socket"])
        except Exception:
            self.provider.shutdown(timeout_millis=plan["runtime"]["shutdown_timeout_ms"])
            raise

    def loss_points(self, _):
        with self.lock:
            return [Observation(value, {"reason": key}) for key, value in self.loss.items()]

    def trace_loss_points(self, _):
        with self.lock:
            return [Observation(value, {"reason": key}) for key, value in self.traces.losses.items()]

    @property
    def observing(self):
        return self.enabled or self.traces is not None

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value
        if self.monitor is not None:
            self.monitor.refresh()

    def reject_observation(self, reason, parent_key, parent_context=None):
        from .traces import SUPPRESSED
        with self.lock:
            self.loss[reason] += 1
            parent = None if parent_key is None else self.pending.get(parent_key)
            context = parent_context if parent_key is None else parent.trace if parent is not None else SUPPRESSED
            self.traces.reject(context, reason)

    def enter(self, key: int, name: str, parent_key=None, parent_context=None):
        if not self.observing or self.closed:
            return
        with self.lock:
            if not self.observing or self.closed:
                return
            parent = parent_context
            if self.traces is not None and parent_key is not None:
                from .traces import SUPPRESSED
                frame = self.pending.get(parent_key)
                parent = frame.trace if frame is not None else SUPPRESSED
            replaced = self.pending.pop(key, None)
            if replaced is not None:
                self.loss["incomplete"] += 1
                if self.traces is not None:
                    self.traces.reject(replaced.trace, "incomplete")
                    self.traces.finish(replaced.trace, time.perf_counter_ns())
            reason = None
            if len(name.encode()) > 1024:
                reason = "function_capacity"
            if name not in self.calls:
                if len(self.calls) >= self.plan["runtime"]["max_functions"]:
                    reason = "function_capacity"
            if len(self.pending) + (self.http.active_count if self.http else 0) >= self.plan["runtime"]["max_active_calls"]:
                reason = reason or "active_call_capacity"
            if reason is not None:
                self.loss[reason] += 1
                if self.traces is not None:
                    self.traces.reject(parent, reason)
                return
            if name not in self.calls:
                self.calls[name] = {"count": 0, "unwinds": 0, "attributes": {"code.function.name": name}}
            started = time.perf_counter_ns()
            context = self.traces.begin(parent, name, started) if self.traces is not None else None
            self.pending[key] = Frame(name, started, self.enabled, context)

    def exit(self, key: int, unwound: bool, cancelled=False):
        ended = time.perf_counter_ns()
        with self.lock:
            pending = self.pending.pop(key, None)
            if pending is None:
                return
            name, start = pending.name, pending.started
            item = self.calls[name]
            if pending.metrics:
                item["count"] += 1
                item["unwinds"] += int(unwound)
                try:
                    self.counter.add(1, item["attributes"])
                    self.duration.record((ended - start) / 1e9, item["attributes"])
                    if unwound:
                        self.unwinds.add(1, item["attributes"])
                except Exception:
                    self.loss["invalid"] += 1
            if self.traces is not None:
                self.traces.finish(pending.trace, ended, unwound, cancelled)
            if not self.observing and not self.pending and self.monitor is not None:
                self.monitor.refresh()

    def bind_control(self, value: str):
        path = Path(value)
        parent = path.parent
        meta = parent.lstat()
        if str(parent) == "." or not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
            raise ValueError("control directory must be owned by this user with mode 0700")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            listener.listen(8)
            self.socket_identity = path.lstat().st_ino
            path.chmod(0o600)
            listener.settimeout(0.05)
        except Exception:
            listener.close()
            raise
        self.socket = listener
        self.control = threading.Thread(target=self.serve, name="otelc-control", daemon=True)
        self.control.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(0.2)
                try:
                    request = bytearray()
                    while len(request) <= 16:
                        byte = connection.recv(1)
                        if byte == b"\n":
                            break
                        if not byte:
                            raise ValueError("incomplete request")
                        request.extend(byte)
                    command = request.decode("ascii")
                    if command not in ("status", "enable", "disable") or len(request) > 16:
                        raise ValueError("invalid request")
                    with self.lock:
                        if command != "status":
                            self.enabled = command == "enable"
                        response = {"schema_version": 1, "pid": os.getpid(), "metrics_enabled": self.enabled, "function_calls": sum(v["count"] for v in self.calls.values())}
                except (OSError, ValueError):
                    response = {"error": "invalid or incomplete control request"}
                try:
                    connection.sendall(json.dumps(response).encode() + b"\n")
                except OSError:
                    pass

    def close(self):
        if self.closed:
            return self.report
        self.closed = True
        deadline = time.monotonic() + self.plan["runtime"]["shutdown_timeout_ms"] / 1000
        self.shutdown_deadline = deadline
        self.stop.set()
        if self.socket:
            self.socket.close()
            self.control.join(max(0, deadline - time.monotonic()))
            path = Path(self.plan["runtime"]["control_socket"])
            if path.exists() and path.lstat().st_ino == self.socket_identity:
                path.unlink()
        with self.lock:
            if self.lifetimes is not None:
                self.lifetimes.shutdown()
            self.loss["incomplete"] += sum(frame.metrics or frame.trace is not None and frame.trace.sampled for frame in self.pending.values())
            self.pending.clear()
            if self.traces is not None:
                self.traces.shutdown_pending()
        self.provider.shutdown(timeout_millis=max(1, (deadline - time.monotonic()) * 1000))
        report = {"schema_version": 1, "language": "python", "pid": os.getpid(), "export_finished": not self.reader._daemon_thread.is_alive(), "function_calls": sum(v["count"] for v in self.calls.values()), "functions": {name: {k: v for k, v in data.items() if k != "attributes"} for name, data in self.calls.items()}, "losses": self.loss, "export_loss": self.export_loss}
        if self.traces is not None:
            report["traces"] = self.traces.report()
        if self.lifetimes is not None:
            report['lifetimes'] = self.lifetimes.report()
        if path := os.environ.get("OTELC_REPORT_PATH"):
            Path(path).write_text(json.dumps(report, indent=2) + "\n")
        self.report = report
        return report
