"""Versioned runtime health shape validation, not a delivery guarantee."""

from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path
from typing import Any

from otelc import python_health
from otelc.python_sdk_io import decode

MAX_REPORT = 65536


def _counter(value: object) -> bool:
    return value is None or isinstance(value, str) and re.fullmatch(r"0|[1-9]\d*", value, re.ASCII) is not None


def _observers(value: object) -> bool:
    return type(value) is int and value >= 0


def _identity(value: Any) -> None:
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("kind") != "otelc.runtime-health"
            or value.get("language") not in ("java", "c", "cpp") or value.get("scope") != "sdk_pipeline"
            or type(value.get("pid")) is not int or value["pid"] <= 0
            or value.get("phase") not in ("running", "final")
            or type(value.get("sdk_attached")) is not bool):
        raise ValueError("invalid runtime health identity/version")
    for key in ("policy_sha256", "selection_sha256"):
        if not isinstance(value.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise ValueError("invalid runtime health digest")


def _span_accounting(accounting: Any) -> None:
    if (not isinstance(accounting, dict)
            or any(not _observers(accounting.get(key)) for key in ("processors_observed", "batch_processors_observed"))
            or accounting.get("queue_evidence") not in ("derived_after_shutdown", "unavailable")):
        raise ValueError("invalid runtime health processor evidence")
    for key in ("sampled_started", "sampled_ended", "sampled_active", "queue_pending", "queue_dropped"):
        if key not in accounting or not _counter(accounting[key]):
            raise ValueError("invalid runtime health span counter")


def _exports(exports: Any, unit: str) -> None:
    if not isinstance(exports, dict) or not _observers(exports.get("exporters_observed")) or exports.get("unit") != unit:
        raise ValueError("invalid runtime health exporter evidence")
    for key in ("batches_started", "batches_succeeded", "batches_failed", "batches_pending",
                "units_started", "units_succeeded", "units_failed"):
        if key not in exports or not _counter(exports[key]):
            raise ValueError("invalid runtime health export counter")


def validate(value: Any) -> dict[str, Any]:
    """Require the public report shape; consumers must assess its actual evidence."""
    if type(value) is dict and value.get('schema_version') == 2:
        return python_health.validate(value)
    _identity(value)
    lifecycle = value.get("lifecycle")
    outcomes = ("not_started", "pending", "succeeded", "failed", "timed_out", "unavailable")
    if not isinstance(lifecycle, dict) or any(lifecycle.get(key) not in outcomes for key in ("flush", "shutdown")):
        raise ValueError("invalid runtime health lifecycle")
    flush = value.get("metric_flush")
    if (not isinstance(flush, dict) or any(not isinstance(flush.get(key), str) or not _counter(flush[key])
                                          for key in ("attempts", "failed_attempts"))):
        raise ValueError("invalid runtime health flush accounting")
    _span_accounting(value.get("span_accounting"))
    _exports(value.get("trace_exports"), "spans")
    _exports(value.get("metric_exports"), "metric_points")
    if value['language'] in ('c', 'cpp'):
        functions = value.get('function_accounting')
        counters = ('registered', 'started', 'ended', 'active', 'lost', 'invalid', 'metadata_errors')
        if (not isinstance(functions, dict) or type(functions.get('closed')) is not bool
                or any(not isinstance(functions.get(key), str) or not _counter(functions[key]) for key in counters)
                or value.get('source_complete') is not False):
            raise ValueError('invalid native function accounting')
    if 'object_accounting' in value:
        objects = value['object_accounting']
        counters = ('types', 'admitted', 'completed', 'live', 'destroying', 'ending',
                    'censored', 'lost', 'duplicates', 'invalid')
        if (value['language'] != 'cpp' or type(objects) is not dict
                or set(objects) != set(counters) | {'closing', 'closed'}
                or any(type(objects.get(key)) is not bool for key in ('closing', 'closed'))
                or any(type(objects.get(key)) is not str or not _counter(objects[key]) for key in counters)):
            raise ValueError('invalid native object accounting')
    if value.get("downstream_receipt") != "unavailable" or value.get("otlp_partial_rejection") != "unavailable":
        raise ValueError("unsupported runtime health delivery claim")
    return value


def read(path: Path) -> dict[str, Any]:
    """Read a bounded private report, including the retained post-exit checkpoint."""
    if path.name != "health.json":
        raise ValueError("expected the generated health.json report")
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError("runtime health directory must be owned and private (0700)")
        data = _read_report(directory)
    finally:
        os.close(directory)
    if len(data) > MAX_REPORT:
        raise ValueError("oversized runtime health report")
    value = decode(data)
    if value.get('schema_version') != 2 and len(data) > 4096:
        raise ValueError('oversized version-one runtime health report')
    return validate(value)


def _read_report(directory: int) -> bytes:
    """Use a fixed basename and check the opened object, avoiding lstat/open races."""
    try:
        descriptor = os.open("health.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("runtime health report cannot be a symlink") from error
        raise
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid != os.getuid() or metadata.st_size > MAX_REPORT):
            raise ValueError("expected a bounded private runtime health report")
        # Retain ownership until the wrapper has been constructed and closed.
        # fdopen itself can raise, for example when given a directory descriptor.
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(MAX_REPORT + 1)
    finally:
        os.close(descriptor)
