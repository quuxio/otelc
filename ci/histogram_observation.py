"""Check nonnegative binary64 histograms against their buckets and extrema."""

from bisect import bisect_left
from fractions import Fraction
import math


def require(condition: bool, reason: str) -> None:
    """Contradictory aggregate fields cannot form a completed observation."""
    if not condition:
        raise ValueError('histogram observation: ' + reason)


def feasible_range(buckets: tuple, minimum: float, maximum: float,
                   extrema: tuple, bounds: list) -> tuple[Fraction, Fraction]:
    """Reserve each actual extreme, then bound the other bucket observations."""
    lower = upper = Fraction(0)
    remaining = list(buckets)
    for value in extrema:
        index = bisect_left(bounds, value)
        remaining[index] -= 1
        require(remaining[index] >= 0, 'histogram extrema have no observations')
        lower += Fraction(value)
        upper += Fraction(value)
    for index, amount in enumerate(remaining):
        if not amount:
            continue
        low = minimum if index == 0 else max(minimum, math.nextafter(bounds[index - 1], math.inf))
        high = maximum if index == len(bounds) else min(maximum, bounds[index])
        require(low <= high, 'histogram populated bucket contradicts its extrema')
        lower += Fraction(low) * amount
        upper += Fraction(high) * amount
    return lower, upper


def rounding_bound(count: int, bound: Fraction) -> Fraction:
    """Bound nonnegative binary64 addition error, including subnormal rounding."""
    additions = count - 1  # The first binary64 value added to zero is exact.
    gamma = Fraction(additions, 2 ** 53 - additions)
    underflow = Fraction(additions, 2 ** 1075) * (1 + gamma)
    return bound * gamma + underflow if bound else Fraction(0)


def histogram_range(point, bounds: list) -> None:
    """Use exact rational bounds and an explicit binary64 summation tolerance."""
    count, minimum, maximum = point.count, point.min, point.max
    require(0 < count < 2 ** 52, 'histogram count exceeds the summation bound')
    extrema = (minimum, maximum) if minimum != maximum else (minimum,)
    lower, upper = feasible_range(point.bucket_counts, minimum, maximum, extrema, bounds)
    total = Fraction(point.sum)
    require(lower - rounding_bound(count, lower) <= total <= upper + rounding_bound(count, upper),
            'histogram sum contradicts its range or populated buckets')


def cumulative_histogram(previous: tuple, sample: tuple, bounds: list) -> None:
    """Only newly added bucket observations can change a cumulative sum."""
    old_minimum, old_maximum = previous[5]
    minimum, maximum = sample[5]
    require(sample[2] >= previous[2]
            and all(current >= old for current, old in zip(sample[3], previous[3], strict=True)),
            'decreasing cumulative histogram buckets or count')
    if sample[2] == previous[2]:
        require(sample[2:] == previous[2:], 'histogram changed without added observations')
        return
    require(minimum <= old_minimum and maximum >= old_maximum,
            'cumulative histogram range lost an earlier extreme')
    required = tuple(value for changed, value in ((minimum < old_minimum, minimum),
                                                 (maximum > old_maximum, maximum)) if changed)
    buckets = tuple(current - old for current, old in zip(sample[3], previous[3], strict=True))
    lower, upper = feasible_range(buckets, minimum, maximum, required, bounds)
    _, old_upper = feasible_range(previous[3], old_minimum, old_maximum,
                                 (old_minimum, old_maximum) if old_minimum != old_maximum else (old_minimum,), bounds)
    _, current_upper = feasible_range(sample[3], minimum, maximum,
                                     (minimum, maximum) if minimum != maximum else (minimum,), bounds)
    error = rounding_bound(previous[2], old_upper) + rounding_bound(sample[2], current_upper)
    added = Fraction(sample[4]) - Fraction(previous[4])
    require(lower - error <= added <= upper + error,
            'cumulative histogram sum contradicts added bucket observations')
