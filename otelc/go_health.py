"""Validate explicit Go SDK checkpoints without claiming downstream delivery."""
from __future__ import annotations

import re
from typing import Any

PROBE_COUNTERS = frozenset((
    'function_started', 'function_ended', 'function_active', 'function_lost',
    'observer_started', 'observer_ended', 'observer_active', 'observer_lost',
    'unwind_unknown', 'object_admitted', 'gc_observed', 'resource_released',
    'objects_pending', 'resources_unreleased', 'lifetime_lost', 'duplicate_objects',
))
MAX_UINT64 = (1 << 64) - 1
OUTCOMES = frozenset(('not_started', 'pending', 'succeeded', 'failed', 'timed_out'))


def counters(value: Any, fields: set[str] | frozenset[str], *, boolean: str | None = None) -> None:
    """Require precise decimal counters; booleans never stand in for numbers."""
    expected = fields | ({boolean} if boolean else set())
    if type(value) is not dict or set(value) != expected:
        raise ValueError('invalid Go health counter fields')
    if boolean and type(value[boolean]) is not bool:
        raise ValueError('invalid Go health observer flag')
    for field in fields:
        counter = value[field]
        if (type(counter) is not str or re.fullmatch(r'0|[1-9]\d{0,19}', counter, flags=re.ASCII) is None
                or int(counter) > MAX_UINT64):
            raise ValueError('invalid Go health unsigned counter')


def exports(value: Any, *, traces: bool) -> None:
    """Absent trace SDKs expose no invented exporter or queue counters."""
    if type(value) is not dict or type(value.get('observed')) is not bool:
        raise ValueError('invalid Go exporter evidence')
    if not value['observed']:
        if value != {'observed': False}:
            raise ValueError('unobserved Go exporter has invented counters')
        return
    fields = {'attempts', 'succeeded', 'failed'}
    if traces:
        fields |= {'exported_spans', 'queue_pending', 'queue_dropped'}
    counters({key: item for key, item in value.items() if key != 'observed'}, fields, boolean='closed')


def controls(state: Any, pid: int) -> None:
    """Require one exact same-process metric switch checkpoint."""
    if (type(state) is not dict or set(state) != {'schema_version', 'pid', 'collection', 'emission', 'effective_emission_off'}
            or type(state['schema_version']) is not int or state['schema_version'] != 1
            or type(state['pid']) is not int or state['pid'] != pid
            or any(type(state[field]) is not bool for field in ('collection', 'emission', 'effective_emission_off'))
            or state['effective_emission_off'] != (not state['emission'])):
        raise ValueError('invalid Go runtime health controls')


def lifecycle(lifecycle: Any) -> None:
    """Keep SDK flush and shutdown outcomes explicit."""
    if (type(lifecycle) is not dict or set(lifecycle) != {'flush', 'shutdown'}
            or any(type(item) is not str or item not in OUTCOMES for item in lifecycle.values())):
        raise ValueError('invalid Go SDK lifecycle evidence')


def http_observation(http: Any, traces_observed: bool) -> None:
    """An HTTP observer belongs to the same owned trace SDK."""
    if type(http) is not dict or type(http.get('observed')) is not bool:
        raise ValueError('invalid Go HTTP span observation')
    if http['observed']:
        counters({key: item for key, item in http.items() if key != 'observed'},
                 {'started', 'ended', 'active'}, boolean='closed')
    elif http != {'observed': False}:
        raise ValueError('unobserved Go HTTP spans have invented counters')
    if http['observed'] != traces_observed:
        raise ValueError('Go HTTP observer differs from its owned trace SDK')


def validate(value: Any) -> dict[str, Any]:
    """Accept schema 4 only; HTTP activity is separate from function probes."""
    fields = {'schema_version', 'kind', 'language', 'scope', 'pid', 'policy_sha256',
              'selection_sha256', 'phase', 'sdk_attached', 'source_complete', 'controls',
              'lifecycle', 'metric_flush', 'probe_accounting', 'metric_exports', 'http_spans',
              'trace_exports', 'downstream_receipt', 'otlp_partial_rejection'}
    if (type(value) is not dict or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 4
            or value['kind'] != 'otelc.runtime-health' or value['language'] != 'go'
            or value['scope'] != 'sdk_pipeline' or type(value['pid']) is not int or value['pid'] <= 0
            or value['phase'] not in ('running', 'final') or value['sdk_attached'] is not True
            or value['source_complete'] is not False):
        raise ValueError('invalid Go runtime health identity')
    for field in ('policy_sha256', 'selection_sha256'):
        if type(value[field]) is not str or re.fullmatch('[0-9a-f]{64}', value[field]) is None:
            raise ValueError('invalid Go runtime health digest')
    controls(value['controls'], value['pid'])
    lifecycle(value['lifecycle'])
    counters(value['metric_flush'], {'attempts', 'failed_attempts'})
    counters(value['probe_accounting'], PROBE_COUNTERS, boolean='closed')
    exports(value['metric_exports'], traces=False)
    exports(value['trace_exports'], traces=True)
    http_observation(value['http_spans'], value['trace_exports']['observed'])
    if (value['metric_exports'].get('observed') is not True
            or value['downstream_receipt'] != 'unavailable' or value['otlp_partial_rejection'] != 'unavailable'):
        raise ValueError('unsupported Go health delivery claim')
    return value


def final_clean(value: Any) -> bool:
    """Assess final SDK drain only; unresolved lifetimes and receipt stay separate."""
    report = validate(value)
    probes = report['probe_accounting']
    http = report['http_spans']
    if http['observed'] and (not http['closed'] or http['active'] != '0' or http['started'] != http['ended']):
        return False
    if (report['phase'] != 'final' or report['lifecycle'] != {'flush': 'succeeded', 'shutdown': 'succeeded'}
            or not probes['closed'] or report['metric_flush']['failed_attempts'] != '0'
            or any(probes[key] != '0' for key in ('function_active', 'observer_active',
                'function_lost', 'observer_lost', 'unwind_unknown', 'lifetime_lost'))
            or probes['function_started'] != probes['function_ended']
            or probes['observer_started'] != probes['observer_ended']):
        return False
    for name in ('metric_exports', 'trace_exports'):
        exports = report[name]
        if not exports['observed']:
            continue
        if (not exports['closed'] or exports['failed'] != '0' or exports['attempts'] != exports['succeeded']
                or name == 'trace_exports' and any(exports[key] != '0' for key in ('queue_pending', 'queue_dropped'))):
            return False
    return True
