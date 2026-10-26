"""Independent concrete prefixes for shared floating histogram accounting."""

import math
import random
from types import SimpleNamespace
import unittest

from ci import histogram_observation as histogram

BOUNDS = [1e-6, 1e-5, .0001, .001, .01, .1, 1.]


def point(values, bounds=BOUNDS):
    """Aggregate actual values without calling the checked range arithmetic."""
    buckets = [0] * (len(bounds) + 1)
    for value in values:
        bucket = next((i for i, bound in enumerate(bounds) if value <= bound), len(bounds))
        buckets[bucket] += 1
    return SimpleNamespace(count=len(values), sum=sum(values), min=min(values), max=max(values), bucket_counts=buckets)


def sample(values, time, bounds=BOUNDS):
    value = point(values, bounds)
    return (1, time, value.count, tuple(value.bucket_counts), value.sum, (value.min, value.max))


class HistogramObservationTests(unittest.TestCase):
    def test_real_prefixes_subnormals_boundaries_rounding_and_large_values(self):
        rng = random.Random(491)
        for bounds in (BOUNDS, [.02, .3]):
            values = []
            previous = None
            for count in range(1, 50):
                values.append(rng.choice([0., math.ulp(0.), 1e-7, .1, 1e300, bounds[0], math.nextafter(bounds[0], math.inf)]))
                histogram.histogram_range(point(values, bounds), bounds)
                current = sample(values, count, bounds)
                if previous is not None:
                    histogram.cumulative_histogram(previous, current, bounds)
                previous = current
        repeated = sample([.1] * 3, 1)
        histogram.cumulative_histogram(repeated, (1, 2, *repeated[2:]), BOUNDS)

    def test_inconsistent_sums_buckets_extrema_and_count_refuse(self):
        for kind in ('sum', 'bucket', 'range', 'zero-count', 'large-count'):
            value = point([.01] * 3)
            if kind == 'sum': value.sum = 1000
            elif kind == 'bucket': value.bucket_counts[:] = [3, 0, 0, 0, 0, 0, 0, 0]
            elif kind == 'range':
                value.min, value.max = .001, .02
                value.bucket_counts[:] = [0, 0, 0, 0, 2, 1, 0, 0]
            elif kind == 'zero-count': value.count = 0
            else: value.count = 2 ** 52
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                histogram.histogram_range(value, BOUNDS)

    def test_cumulative_prefixes_cannot_lose_or_replace_earlier_observations(self):
        for kind in ('count', 'bucket', 'range', 'unchanged-count', 'added-high', 'added-low'):
            previous = sample([.01] * 3, 100)
            current = sample([.01, .01, .03, .03, .03, .03], 200)
            if kind == 'count': current = sample([.01], 200)
            elif kind == 'range':
                previous = sample([.004, .01, .01], 100)
                current = sample([.005] * 5 + [.01], 200)
            elif kind == 'unchanged-count':
                current = (1, 200, *previous[2:4], math.nextafter(previous[4], math.inf), previous[5])
            elif kind.startswith('added-'):
                previous = sample([.002, .004, .01], 100)
                current = sample([.002] * 5 + [.01], 200)
                current = (*current[:4], .05 if kind.endswith('high') else .02, current[5])
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                histogram.cumulative_histogram(previous, current, BOUNDS)


if __name__ == '__main__':
    unittest.main()
