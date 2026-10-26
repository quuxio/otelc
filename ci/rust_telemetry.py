"""Join declared prepared Rust fixtures to actual decoded receipt and SDK units."""
from __future__ import annotations

from collections import Counter
import math
from typing import Any

from otelc import native_benchmark
from otelc.config import Policy
from ci import histogram_observation as histogram


def spans(capture: Any, service: str) -> list[Any]:
    """Keep a unique fixture service separate from other genuine suite traffic."""
    return [span for request in capture.traces for resource in request.resource_spans
            if any(item.key == 'service.name' and item.value.string_value == service
                   for item in resource.resource.attributes)
            for scope in resource.scope_spans for span in scope.spans]


def points(capture: Any, service: str) -> list[tuple[str, str, Any]]:
    """Keep actual point units, including empty API exports sending no HTTP body."""
    result = []
    for request in capture.metrics:
        for resource in request.resource_metrics:
            if not any(item.key == 'service.name' and item.value.string_value == service
                       for item in resource.resource.attributes):
                continue
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    kind = metric.WhichOneof('data')
                    if kind not in ('sum', 'histogram', 'gauge'):
                        raise ValueError('prepared Rust receipt has an unsupported metric data kind')
                    if (kind in ('sum', 'histogram')
                            and (getattr(metric, kind).aggregation_temporality != 2
                                 or kind == 'sum' and not metric.sum.is_monotonic)):
                        raise ValueError('prepared Rust receipt requires cumulative monotonic counters/histograms')
                    result.extend((metric.name, kind, point) for point in getattr(metric, kind).data_points)
    return result


def calls(capture: Any, service: str) -> int:
    """Read cumulative calls without confusing SDK batch counters with receipt."""
    observed = [point.as_int for name, kind, point in points(capture, service)
                if name == 'otelc.function.calls' and kind == 'sum' and point.WhichOneof('value') == 'as_int']
    return max(observed, default=0)


def duration(point: Any, bounds: list[float]) -> tuple:
    """Validate complete nonnegative duration state before rational range checks."""
    if (point.flags or not 0 < point.count < 2**52 or len(point.bucket_counts) != len(bounds) + 1
            or sum(point.bucket_counts) != point.count or list(point.explicit_bounds) != bounds
            or any(not point.HasField(key) for key in ('sum', 'min', 'max'))
            or any(not math.isfinite(getattr(point, key)) or getattr(point, key) < 0 for key in ('sum', 'min', 'max'))
            or point.min > point.max):
        raise ValueError('prepared Rust duration has incomplete, nonfinite or contradictory state')
    histogram.histogram_range(point, bounds)
    return (point.start_time_unix_nano, point.time_unix_nano, point.count,
            tuple(point.bucket_counts), point.sum, (point.min, point.max))


def cumulative(observed: list[tuple[str, str, Any]], bounds: list[float]) -> None:
    """Check one unchanged SDK's chronological series, allowing HTTP reordering."""
    series: dict[tuple, list[tuple]] = {}
    for name, kind, point in observed:
        if point.flags:
            raise ValueError('prepared Rust metric flags do not describe a recorded value')
        if kind == 'gauge':
            if point.time_unix_nano <= 0:
                raise ValueError('prepared Rust gauge observation time is unavailable')
            continue
        attributes = tuple(sorted((item.key, item.value.string_value) for item in point.attributes))
        sample = duration(point, bounds) if kind == 'histogram' else (
            point.start_time_unix_nano, point.time_unix_nano, point.as_int)
        if not 0 < sample[0] <= sample[1]:
            raise ValueError('prepared Rust metric timestamps are unavailable or reversed')
        series.setdefault((name, kind, attributes), []).append(sample)
    for (_, kind, _), samples in series.items():
        samples.sort(key=lambda value: (value[0], value[1]))
        previous = None
        for sample in samples:
            if previous is not None:
                if sample[0] != previous[0]:
                    raise ValueError('prepared Rust cumulative start changed within one SDK')
                if sample[1] == previous[1] and sample[2:] != previous[2:]:
                    raise ValueError('prepared Rust cumulative state changed at the same observation time')
                if kind == 'histogram':
                    histogram.cumulative_histogram(previous, sample, bounds)
                elif kind == 'sum' and sample[2] < previous[2]:
                    raise ValueError('prepared Rust cumulative counter decreased')
            previous = sample


def lifetime_metrics(observed: list[tuple[str, str, Any]], expected: dict[str, int], bounds: list[float]) -> None:
    """Require each declared root-local type's admitted/completed/duration/live data."""
    types = {name.removesuffix(' lifetime'): count for name, count in expected.items()}
    kinds = {'admitted': 'sum', 'completed': 'sum', 'duration': 'histogram', 'live': 'gauge'}
    maxima: dict[tuple[str, str], int] = {}
    for name, kind, point in observed:
        if not name.startswith('otelc.lifetime.'):
            continue
        instrument = name.removeprefix('otelc.lifetime.')
        attributes = sorted((item.key, item.value.string_value) for item in point.attributes)
        typename = dict(attributes).get('type')
        if (typename not in types or instrument not in kinds or kind != kinds[instrument]
                or attributes != [('boundary', 'drop_entry'), ('type', typename)]):
            raise ValueError('prepared Rust lifetime metric type, boundary or instrument differs')
        if kind == 'histogram':
            duration(point, bounds)
            value = point.count
            valid = (list(point.explicit_bounds) == bounds
                     and len(point.bucket_counts) == len(bounds) + 1 and sum(point.bucket_counts) == value)
        else:
            value = point.as_int
            valid = point.WhichOneof('value') == 'as_int'
        if not valid or not 0 <= value <= (0 if instrument == 'live' else types[typename]):
            raise ValueError('prepared Rust lifetime metric lost its value or histogram denominator')
        key = instrument, typename
        maxima[key] = max(maxima.get(key, 0), value)
    wanted = {(instrument, typename): 0 if instrument == 'live' else count
              for typename, count in types.items() for instrument in kinds}
    if maxima != wanted:
        raise ValueError('prepared Rust lifetime metric receipt is incomplete')


def verify(capture: Any, policy: Policy, health: dict, *, function: str,
           expected_spans: int, expected_calls: int, lifetimes: dict[str, int] | None = None) -> dict:
    """Check exact fixture span trees and receipt units after a genuine clean drain."""
    native_benchmark.validate_drain(health, policy)
    service = policy.data['resource']['service_name']
    observed_spans = spans(capture, service)
    observed_points = points(capture, service)
    roots = [span for span in observed_spans if span.name == function]
    expected_lifetimes = lifetimes or {}
    if (not policy.data['traces']['enabled'] or policy.data['traces']['sample_ratio'] != 1
            or any(int(health['function_accounting'][key]) != expected_spans for key in ('started', 'ended'))
            or int(health['function_accounting']['registered']) != 1):
        raise ValueError('prepared Rust declared calls differ from the final selected function engine')
    if expected_lifetimes:
        if (any(int(health['drop_accounting'][key]) != sum(expected_lifetimes.values())
                for key in ('admitted', 'completed'))
                or int(health['drop_accounting']['types']) != len(expected_lifetimes)):
            raise ValueError('prepared Rust declared lifetimes differ from the final drop engine')
    root_ids = {(span.trace_id, span.span_id) for span in roots}
    if (len(roots) != expected_spans or len(root_ids) != expected_spans
            or len({(span.trace_id, span.span_id) for span in observed_spans}) != len(observed_spans)
            or any(len(span.trace_id) != 16 or len(span.span_id) != 8
                   or not any(span.trace_id) or not any(span.span_id) for span in observed_spans)
            or any(span.parent_span_id for span in roots)
            or len(observed_spans) != expected_spans + sum(expected_lifetimes.values())):
        raise ValueError('prepared Rust receipt lacks the declared unique root span denominator')
    lifetimes_seen: Counter[str] = Counter()
    for span in observed_spans:
        if span.name == function:
            continue
        if (span.parent_span_id or len(span.links) != 1
                or (span.links[0].trace_id, span.links[0].span_id) not in root_ids):
            raise ValueError('prepared Rust lifetime receipt lost its original function link')
        lifetimes_seen[span.name] += 1
    if dict(lifetimes_seen) != expected_lifetimes:
        raise ValueError('prepared Rust lifetime receipt differs from its exact type/count denominator')
    lifetime_metrics(observed_points, expected_lifetimes, policy.data['metrics']['histogram_boundaries_seconds'])
    functions = [(name, kind, point) for name, kind, point in observed_points
                 if name.startswith('otelc.function.')]
    if (not functions or calls(capture, service) != expected_calls
            or any(name not in ('otelc.function.calls', 'otelc.function.duration')
                   or [(item.key, item.value.string_value) for item in point.attributes] != [('function', function)]
                   for name, _, point in functions)):
        raise ValueError('prepared Rust function metrics differ from the declared completed calls')
    duration_counts = []
    for name, kind, point in functions:
        if name == 'otelc.function.calls':
            valid = kind == 'sum' and point.WhichOneof('value') == 'as_int' and 0 < point.as_int <= expected_calls
        else:
            valid = (kind == 'histogram' and 0 < point.count <= expected_calls
                     and len(point.bucket_counts) == len(point.explicit_bounds) + 1
                     and sum(point.bucket_counts) == point.count
                     and list(point.explicit_bounds) == policy.data['metrics']['histogram_boundaries_seconds'])
            if valid:
                duration_counts.append(point.count)
        if not valid:
            raise ValueError('prepared Rust metric point lost its cumulative value or histogram denominator')
    if max(duration_counts, default=0) != expected_calls:
        raise ValueError('prepared Rust metric receipt lacks its final cumulative duration')
    cumulative(observed_points, policy.data['metrics']['histogram_boundaries_seconds'])
    units = {'spans': len(observed_spans), 'metric_points': len(observed_points)}
    for key, count in (('trace_exports', units['spans']), ('metric_exports', units['metric_points'])):
        if int(health[key]['units_succeeded']) != count:
            raise ValueError('prepared Rust downstream receipt differs from the final SDK unit count')
    return {'schema_version': 1, 'kind': 'otelc.rust.fixture-receipt', 'service': service,
            'function': function, 'function_spans': len(roots), 'lifetime_spans': dict(lifetimes_seen),
            'cumulative_calls': expected_calls, 'received_units': units,
            'scope': 'Declared test fixture and received entities; not general oteleq or collector forwarding proof'}
