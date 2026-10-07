"""Bounded application identities, SDK metrics and owner-only live admission control."""
import json
import os
import socket
import stat
import threading
import time
import urllib.parse
from pathlib import Path
import requests

from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceResponse


class Session(requests.Session):
    """Reject invalid/partial OTLP success and keep response bodies out of SDK logs."""
    def post(self, *args, **kwargs):
        kwargs.update(allow_redirects=False, stream=True)
        response = super().post(*args, **kwargs)
        try:
            if response.status_code == 200:
                body = response.raw.read(65537, decode_content=True)
                result = ExportMetricsServiceResponse.FromString(body)
                if len(body) > 65536 or result.partial_success.rejected_data_points:
                    raise ValueError("invalid or partially rejected OTLP batch")
        except Exception:
            response.status_code = 422
            response.reason = "invalid or partially rejected OTLP batch"
        finally:
            response.close()
            response._content = b""
        return response


def export_headers() -> dict[str, str]:
    value = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_HEADERS", os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))
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
            for scope in resource.scope_metrics for metric in scope.metrics
            if metric.name != "otelc.export.dropped_batches")
        if signature == self.signature:
            return MetricExportResult.SUCCESS
        try:
            result = self.delegate.export(metrics_data, timeout_millis=min(timeout_millis, runtime.timeout_ms))
        except Exception:
            result = MetricExportResult.FAILURE
        if result != MetricExportResult.SUCCESS:
            runtime.export_loss += 1
        else:
            self.signature = signature
        return result

    def force_flush(self, timeout_millis=10000):
        return self.delegate.force_flush(timeout_millis=timeout_millis)

    def shutdown(self, timeout_millis=30000, **kwargs):
        self.delegate.shutdown(timeout_millis=timeout_millis)


class Runtime:
    def __init__(self, plan: dict, delegate=None):
        self.closed = False
        self.monitor = None
        self.report = None
        self.plan = plan
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
        if delegate is None:
            delegate = OTLPMetricExporter(endpoint=plan["metrics_endpoint"], headers=export_headers(), timeout=self.timeout_ms / 1000, session=Session())
        self.exporter = Exporter(self, delegate)
        self.reader = PeriodicExportingMetricReader(self.exporter, export_interval_millis=plan["export"]["interval_ms"], export_timeout_millis=self.timeout_ms)
        identity = {"service.name": plan["resource"]["service_name"], "service.version": plan["resource"]["service_version"], "service.instance.id": str(os.getpid())}
        identity.update(plan["resource"]["attributes"])
        self.provider = MeterProvider(resource=Resource(identity), metric_readers=[self.reader], shutdown_on_exit=False, views=[View(instrument_name="otelc.function.duration", aggregation=ExplicitBucketHistogramAggregation(plan["metrics"]["histogram_boundaries_seconds"]))])
        meter = self.provider.get_meter("quux.otelc", "0.1.0")
        self.counter = meter.create_counter("otelc.function.calls", unit="{call}")
        self.unwinds = meter.create_counter("otelc.function.unwinds", unit="{observation}")
        self.duration = meter.create_histogram("otelc.function.duration", unit="s")
        meter.create_observable_counter("otelc.runtime.dropped_observations", callbacks=[self.loss_points], unit="{observation}")
        meter.create_observable_counter("otelc.export.dropped_batches", callbacks=[lambda _: [Observation(self.export_loss)]], unit="{batch}")
        try:
            if plan["runtime"]["control_socket"]:
                self.bind_control(plan["runtime"]["control_socket"])
        except Exception:
            self.provider.shutdown(timeout_millis=plan["runtime"]["shutdown_timeout_ms"])
            raise

    def loss_points(self, _):
        with self.lock:
            return [Observation(value, {"reason": key}) for key, value in self.loss.items()]

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value
        if self.monitor is not None:
            self.monitor.refresh()

    def enter(self, key: int, name: str):
        if not self.enabled or self.closed:
            return
        with self.lock:
            if not self.enabled or self.closed:
                return
            if self.pending.pop(key, None) is not None:
                self.loss["incomplete"] += 1
            if name not in self.calls:
                if len(self.calls) >= self.plan["runtime"]["max_functions"]:
                    self.loss["function_capacity"] += 1
                    return
                self.calls[name] = {"count": 0, "unwinds": 0, "attributes": {"code.function.name": name}}
            if len(self.pending) >= self.plan["runtime"]["max_active_calls"]:
                self.loss["active_call_capacity"] += 1
                return
            self.pending[key] = (name, time.perf_counter_ns())

    def exit(self, key: int, unwound: bool):
        ended = time.perf_counter_ns()
        with self.lock:
            pending = self.pending.pop(key, None)
            if pending is None:
                return
            name, start = pending
            item = self.calls[name]
            item["count"] += 1
            item["unwinds"] += int(unwound)
            try:
                self.counter.add(1, item["attributes"])
                self.duration.record((ended - start) / 1e9, item["attributes"])
                if unwound:
                    self.unwinds.add(1, item["attributes"])
            except Exception:
                self.loss["invalid"] += 1
            if not self.enabled and not self.pending and self.monitor is not None:
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
        self.stop.set()
        if self.socket:
            self.socket.close()
            self.control.join(max(0, deadline - time.monotonic()))
            path = Path(self.plan["runtime"]["control_socket"])
            if path.exists() and path.lstat().st_ino == self.socket_identity:
                path.unlink()
        with self.lock:
            self.loss["incomplete"] += len(self.pending)
            self.pending.clear()
        self.provider.shutdown(timeout_millis=max(1, (deadline - time.monotonic()) * 1000))
        report = {"schema_version": 1, "language": "python", "pid": os.getpid(), "export_finished": not self.reader._daemon_thread.is_alive(), "function_calls": sum(v["count"] for v in self.calls.values()), "functions": {name: {k: v for k, v in data.items() if k != "attributes"} for name, data in self.calls.items()}, "losses": self.loss, "export_loss": self.export_loss}
        if path := os.environ.get("OTELC_REPORT_PATH"):
            Path(path).write_text(json.dumps(report, indent=2) + "\n")
        self.report = report
        return report
