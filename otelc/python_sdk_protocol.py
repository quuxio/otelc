"""Closed primitive records for the Python SDK process boundary."""

from __future__ import annotations

import math
import re
from typing import Any

MAX_TEXT = 4096
MAX_ATTRIBUTES = 128
MAX_INSTRUMENTS = 4096
COUNTERS = ('counter', 'up_down_counter', 'histogram', 'gauge')


def fields(value: Any, required: set[str], optional: set[str] | None = None
           ) -> dict[str, Any]:
    """Refuse missing or unknown protocol fields."""
    if (type(value) is not dict or not required <= value.keys()
            or value.keys() - required - (optional or set())):
        raise ValueError('invalid Python SDK record fields')
    return value


def text(value: Any, *, limit: int = MAX_TEXT, empty: bool = False) -> str:
    """Bound primitive text without calling application formatters."""
    if (type(value) is not str or not empty and not value
            or len(value.encode('utf8')) > limit):
        raise ValueError('invalid Python SDK record text')
    return value


def integer(value: Any, *, maximum: int = 2 ** 63 - 1,
            minimum: int = 0) -> int:
    """Keep booleans and out-of-range integers out of numeric protocol fields."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('invalid Python SDK record integer')
    return value


def flag(value: Any) -> bool:
    """A live switch is an exact JSON boolean."""
    if type(value) is not bool:
        raise ValueError('invalid Python SDK record boolean')
    return value


def number(value: Any) -> int | float:
    """Accept finite SDK measurements with a representable integer range."""
    if type(value) is int:
        return integer(value, minimum=-(2 ** 63))
    if type(value) is not float or not math.isfinite(value):
        raise ValueError('invalid Python SDK measurement')
    return value


def attribute_value(value: Any) -> Any:
    """Keep attributes on the ordinary scalar/homogeneous-array API profile."""
    if type(value) is str:
        return text(value, empty=True)
    if type(value) in (bool, int, float):
        return value if type(value) is bool else number(value)
    if type(value) is list and len(value) <= 128:
        if any(type(item) is list or type(item) is not type(value[0])
               for item in value):
            raise ValueError('invalid Python SDK attribute array')
        return [attribute_value(item) for item in value]
    raise ValueError('unsupported Python SDK attribute value')


def attributes(value: Any) -> dict[str, Any]:
    """Validate primitive attributes before they reach SDK aggregation."""
    if type(value) is not dict or len(value) > MAX_ATTRIBUTES:
        raise ValueError('invalid Python SDK attributes')
    return {text(key, limit=1024): attribute_value(item)
            for key, item in value.items()}


def context(value: Any) -> dict[str, Any] | None:
    """Validate a source-owned OTel context without generating replacement IDs."""
    if value is None:
        return None
    fields(value, {'trace_id', 'span_id', 'trace_flags', 'remote'}, {'trace_state'})
    for key, width in (('trace_id', 32), ('span_id', 16)):
        encoded = text(value[key], limit=width)
        if not re.fullmatch('[0-9a-f]{' + str(width) + '}', encoded) or int(encoded, 16) == 0:
            raise ValueError('invalid Python SDK context identity')
    integer(value['trace_flags'], maximum=255)
    flag(value['remote'])
    text(value.get('trace_state', ''), limit=512, empty=True)
    return value


def metric(record: dict[str, Any]) -> dict[str, Any]:
    """Keep one metric registration and measurement explicit on the wire."""
    fields(record, {'kind', 'scope', 'instrument', 'name', 'value'},
           {'version', 'schema_url', 'unit', 'description', 'attributes',
            'scope_attributes', 'context'})
    text(record['scope'], limit=1024)
    if record['instrument'] not in COUNTERS:
        raise ValueError('unsupported Python SDK instrument')
    name = text(record['name'], limit=255)
    if not re.fullmatch(r'[a-zA-Z][-_./a-zA-Z0-9]{0,254}', name):
        raise ValueError('invalid Python SDK instrument name')
    amount = number(record['value'])
    if record['instrument'] in ('counter', 'histogram') and amount < 0:
        raise ValueError('invalid negative Python SDK measurement')
    for key, limit in (('version', 128), ('schema_url', 1024),
                       ('unit', 63), ('description', 1024)):
        text(record.get(key, ''), limit=limit, empty=True)
    if not record.get('unit', '').isascii():
        raise ValueError('invalid Python SDK instrument unit')
    attributes(record.get('attributes', {}))
    attributes(record.get('scope_attributes', {}))
    context(record.get('context'))
    return record


def span(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one completed span and its existing parent/links/events."""
    fields(record, {'name', 'context', 'start_time', 'end_time'},
           {'parent', 'attributes', 'events', 'links', 'scope', 'version',
            'schema_url', 'scope_attributes', 'status', 'description',
            'span_kind', 'dropped_attributes', 'dropped_events', 'dropped_links'})
    text(record['name'])
    active = context(record['context'])
    if active is None or active['remote']:
        raise ValueError('completed Python SDK span needs a local identity')
    parent = context(record.get('parent'))
    if parent and (parent['trace_id'] != active['trace_id']
                   or parent['span_id'] == active['span_id']):
        raise ValueError('invalid Python SDK span parent')
    started = integer(record['start_time'], minimum=1)
    integer(record['end_time'], minimum=started)
    attributes(record.get('attributes', {}))
    attributes(record.get('scope_attributes', {}))
    text(record.get('scope', 'io.quux.otelc.functions'), limit=1024)
    for key in ('version', 'schema_url', 'description'):
        text(record.get(key, ''), limit=1024, empty=True)
    if record.get('status', 'UNSET') not in ('UNSET', 'OK', 'ERROR'):
        raise ValueError('invalid Python SDK span status')
    if record.get('span_kind', 'INTERNAL') not in (
        'INTERNAL', 'SERVER', 'CLIENT', 'PRODUCER', 'CONSUMER',
    ):
        raise ValueError('invalid Python SDK span kind')
    for key in ('dropped_attributes', 'dropped_events', 'dropped_links'):
        integer(record.get(key, 0), maximum=2 ** 32 - 1)
    span_events(record.get('events', []))
    span_links(record.get('links', []))
    return record


def span_events(value: Any) -> None:
    """Bound existing event data without formatting an exception object."""
    if type(value) is not list or len(value) > 128:
        raise ValueError('invalid Python SDK span events')
    for event in value:
        fields(event, {'name', 'timestamp'}, {'attributes', 'dropped_attributes'})
        text(event['name'])
        integer(event['timestamp'], minimum=1)
        attributes(event.get('attributes', {}))
        integer(event.get('dropped_attributes', 0), maximum=2 ** 32 - 1)


def span_links(value: Any) -> None:
    """Keep link identities distinct from a span's parent."""
    if type(value) is not list or len(value) > 128:
        raise ValueError('invalid Python SDK span links')
    for link in value:
        fields(link, {'context'}, {'attributes', 'dropped_attributes'})
        if context(link['context']) is None:
            raise ValueError('invalid Python SDK span link')
        attributes(link.get('attributes', {}))
        integer(link.get('dropped_attributes', 0), maximum=2 ** 32 - 1)
