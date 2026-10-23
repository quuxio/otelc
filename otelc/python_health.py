"""Common health presentation of observed Python application/SDK evidence."""

from __future__ import annotations

import os
import re
from typing import Any

COUNTERS = ('batches_started', 'batches_succeeded', 'batches_failed',
            'batches_pending', 'units_started', 'units_succeeded', 'units_failed')


def _count(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError('invalid observed Python health counter')
    return str(value)


def _exports(sdk: dict | None, *, traces: bool) -> dict:
    result = {'exporters_observed': None if sdk is None else 1,
              'unit': 'spans' if traces else 'metric_points',
              **dict.fromkeys(COUNTERS)}
    if sdk is None:
        return result
    source = sdk['traces'] if traces else sdk
    names = ('started', 'succeeded', 'failed', 'pending') if traces else (
        'export_started', 'export_succeeded', 'export_failed', 'in_flight_exports')
    required = (*names, 'started_items', 'exported_items', 'failed_items') if traces else names
    if type(source) is not dict or any(key not in source for key in required):
        raise ValueError('incomplete Python health exporter snapshot')
    for name, key in zip(COUNTERS[:4], names):
        result[name] = _count(source[key])
    if traces:
        for name, key in zip(COUNTERS[4:], ('started_items', 'exported_items', 'failed_items')):
            result[name] = _count(source[key])
    # The metric observer counts API batches, not individual metric points.
    return result


def _source(sdk: dict | None, sampled: dict | None, *, pid: int | None,
            sdk_pid: int | None, launch_id: str | None, policy_sha256: str) -> dict | None:
    if sdk is not None and type(sdk) is not dict:
        raise ValueError('invalid Python health SDK snapshot')
    if sampled is not None and (type(sampled) is not dict or any(
            type(sampled.get(key)) is not int or sampled[key] < 0
            for key in ('sampled_started', 'sampled_ended'))):
        raise ValueError('invalid Python health application processor snapshot')
    if sdk is not None and (sdk.get('pid') != pid or sdk.get('sdk_pid') != sdk_pid
            or sdk.get('launch_id') != launch_id or sdk.get('policy_sha256') != policy_sha256):
        raise ValueError('Python health SDK identity differs')
    accounting = sdk.get('span_accounting') if sdk is not None else None
    initialized = accounting is not None
    if initialized and (type(accounting) is not dict or any(key not in accounting
            for key in ('queue_pending', 'queue_dropped', 'queue_evidence'))
            or any(key not in sdk for key in ('traces', 'flush_started', 'flush_failed'))):
        raise ValueError('incomplete Python health pipeline snapshot')
    return accounting


def _spans_view(accounting: dict | None, sampled: dict | None) -> dict:
    initialized = accounting is not None
    return {'processors_observed': 1 if initialized else None,
             'batch_processors_observed': 1 if initialized else None,
             'sampled_started': _count(sampled['sampled_started']) if sampled else None,
             'sampled_ended': _count(sampled['sampled_ended']) if sampled else None,
             'sampled_active': _count(sampled['sampled_started'] - sampled['sampled_ended']) if sampled else None,
             'queue_pending': _count(accounting['queue_pending']) if initialized else None,
             'queue_dropped': _count(accounting['queue_dropped']) if initialized else None,
             'queue_evidence': accounting['queue_evidence'] if initialized else 'unavailable'}


def present(*, pid: int | None, sdk_pid: int | None, launch_id: str | None,
            policy_sha256: str, phase: str, sdk: dict | None,
            application: dict | None, sampled: dict | None,
            application_exit: int | None = None, sdk_exit: int | None = None,
            failures: dict | None = None) -> dict:
    """Do not infer an absent observer, successful flush or downstream receipt."""
    accounting = _source(sdk, sampled, pid=pid, sdk_pid=sdk_pid,
                         launch_id=launch_id, policy_sha256=policy_sha256)
    initialized = accounting is not None
    pipeline = sdk if initialized else None
    shutdown = 'unavailable'
    if phase == 'final' and sdk is not None and type(sdk.get('shutdown_completed')) is bool:
        shutdown = 'succeeded' if sdk['shutdown_completed'] else 'failed'
    result = {'schema_version': 2, 'kind': 'otelc.runtime-health', 'language': 'python',
        'scope': 'application_sdk_pair', 'pid': pid, 'sdk_pid': sdk_pid,
        'launch_id': launch_id, 'policy_sha256': policy_sha256, 'phase': phase,
        'sdk_attached': initialized, 'lifecycle': {'flush': 'unavailable', 'shutdown': shutdown},
        'metric_flush': {'scope': 'exporter',
            'attempts': _count(pipeline['flush_started']) if initialized else None,
            'failed_attempts': _count(pipeline['flush_failed']) if initialized else None},
        'span_accounting': _spans_view(accounting, sampled), 'trace_exports': _exports(pipeline, traces=True),
        'metric_exports': _exports(pipeline, traces=False),
        'downstream_receipt': 'unavailable', 'otlp_partial_rejection': 'unavailable',
        'source_complete': False, 'application_exit': application_exit, 'sdk_exit': sdk_exit,
        'failures': dict(failures or {}), 'application': application,
        'sdk': sdk}
    return validate(result)


def live(runtime: Any) -> dict:
    """Read current facts without a control switch, collection or forced export."""
    with runtime._lock:
        application = runtime.status(observe_lifetimes=False)
        application['lifetime_events_pending'] = (
            runtime.lifetimes.pending_events() if runtime.lifetimes is not None else None)
        sampled = runtime._processor.snapshot()[0] if runtime._processor is not None else None
    sdk = runtime.channel.request('status', timeout=2.5)
    return present(pid=os.getpid(), sdk_pid=runtime.channel.sdk_pid,
        launch_id=runtime.channel.launch_id, policy_sha256=runtime.channel.policy_sha256,
        phase='running', sdk=sdk, application=application, sampled=sampled)


def _counter(value: object) -> bool:
    return value is None or type(value) is str and re.fullmatch(r'0|[1-9][0-9]*', value) is not None


def _identity(value: Any) -> None:
    if (type(value) is not dict or type(value.get('schema_version')) is not int
            or value['schema_version'] != 2 or value.get('kind') != 'otelc.runtime-health'
            or value.get('language') != 'python' or value.get('scope') != 'application_sdk_pair'
            or value.get('phase') not in ('running', 'final')
            or type(value.get('sdk_attached')) is not bool
            or type(value.get('source_complete')) is not bool or value['source_complete']
            or value.get('downstream_receipt') != 'unavailable'
            or value.get('otlp_partial_rejection') != 'unavailable'):
        raise ValueError('invalid Python health version or scope')
    for key in ('pid', 'sdk_pid'):
        field = value.get(key)
        if (field is None and value['phase'] == 'running'
                or field is not None and (type(field) is not int or field <= 0)):
            raise ValueError('invalid Python health process identity')
    for key, size in (('policy_sha256', 64), ('launch_id', 32)):
        field = value.get(key)
        if field is None and key == 'launch_id' and value['phase'] == 'final':
            continue
        if type(field) is not str or re.fullmatch('[0-9a-f]{' + str(size) + '}', field) is None:
            raise ValueError('invalid Python health digest or launch identity')


def _lifecycle(value: dict) -> None:
    lifecycle = value.get('lifecycle')
    if (type(lifecycle) is not dict or lifecycle.get('flush') != 'unavailable'
            or lifecycle.get('shutdown') not in ('unavailable', 'succeeded', 'failed')):
        raise ValueError('invalid Python health lifecycle evidence')
    flush = value.get('metric_flush')
    if (type(flush) is not dict or flush.get('scope') != 'exporter'
            or any(key not in flush or not _counter(flush[key]) for key in ('attempts', 'failed_attempts'))):
        raise ValueError('invalid Python health flush counts')


def _span_accounting(value: dict) -> None:
    spans = value.get('span_accounting')
    if type(spans) is not dict or spans.get('queue_evidence') not in ('unavailable', 'derived_after_shutdown'):
        raise ValueError('invalid Python health span evidence')
    for key in ('processors_observed', 'batch_processors_observed'):
        if key not in spans or spans[key] is not None and (type(spans[key]) is not int or spans[key] < 0):
            raise ValueError('invalid Python health observer count')
    for key in ('sampled_started', 'sampled_ended', 'sampled_active', 'queue_pending', 'queue_dropped'):
        if key not in spans or not _counter(spans[key]):
            raise ValueError('invalid Python health span count')


def _export_counts(value: dict) -> None:
    for name, unit in (('trace_exports', 'spans'), ('metric_exports', 'metric_points')):
        export = value.get(name)
        if (type(export) is not dict or export.get('unit') != unit
                or 'exporters_observed' not in export
                or export['exporters_observed'] is not None and (
                    type(export['exporters_observed']) is not int or export['exporters_observed'] < 0)
                or any(key not in export or not _counter(export[key]) for key in COUNTERS)):
            raise ValueError('invalid Python health export counts')


def _raw(value: dict) -> dict:
    for key in ('application_exit', 'sdk_exit'):
        if key not in value or value[key] is not None and type(value[key]) is not int:
            raise ValueError('invalid Python health exit status')
    if type(value.get('failures')) is not dict:
        raise ValueError('invalid Python health failure evidence')
    for key in ('sdk', 'application'):
        if key not in value or value[key] is not None and type(value[key]) is not dict:
            raise ValueError('invalid Python health raw evidence')
    sdk = value['sdk']
    if sdk is not None:
        if (type(sdk.get('schema_version')) is not int or sdk['schema_version'] != 1
                or sdk.get('kind') != 'otelc.python.sdk'
                or sdk.get('phase') not in ('waiting_application', 'running', 'closing', 'closed', 'failed')
                or any(type(sdk.get(key)) is not type(value[key]) or sdk[key] != value[key]
                       for key in ('pid', 'sdk_pid', 'policy_sha256', 'launch_id'))):
            raise ValueError('Python health nested SDK identity differs')
    return value


def validate(value: Any) -> dict:
    """Validate presentation, without certifying clean execution or delivery."""
    _identity(value)
    _lifecycle(value)
    _span_accounting(value)
    _export_counts(value)
    return _raw(value)
