"""Isolate owned SDK IDs, jitter and exemplars from application random state."""
from _random import Random
from functools import partial

from opentelemetry.exporter.otlp.common.http import _OTLPHTTPClient
from opentelemetry.sdk.metrics import Counter, Histogram, ObservableCounter, ObservableGauge
from opentelemetry.sdk.metrics import SimpleFixedSizeExemplarReservoir
from opentelemetry.sdk.metrics._internal.exemplar.exemplar_reservoir import BucketIndexError
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.trace.id_generator import IdGenerator


class PrivateRandom:
    """Own a CPython generator without using or patching random module methods."""

    def __init__(self) -> None:
        generator = Random()
        self.bits = generator.getrandbits
        self.unit = generator.random

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self.unit()

    def below(self, limit: int) -> int:
        width = limit.bit_length()
        value = self.bits(width)
        while value >= limit:
            value = self.bits(width)
        return value

    def nonzero(self, width: int) -> int:
        value = self.bits(width)
        while value == 0:
            value = self.bits(width)
        return value


class PrivateIdGenerator(IdGenerator):
    """Retain the SDK's uniformly random, nonzero ID contract per provider."""

    def __init__(self) -> None:
        self.random = PrivateRandom()

    def generate_span_id(self) -> int:
        return self.random.nonzero(64)

    def generate_trace_id(self) -> int:
        return self.random.nonzero(128)

    def is_trace_id_random(self) -> bool:
        return True


def isolate_exporter(exporter) -> None:
    """Replace jitter only on an owned, pinned SDK HTTP client instance."""
    client = exporter._client
    if type(client) is not _OTLPHTTPClient:
        raise ValueError('unsupported Python SDK HTTP client')
    random = PrivateRandom()
    jitter = client._jitter
    client._compute_backoff = lambda retry: 2 ** retry * random.uniform(1 - jitter, 1 + jitter)


class PrivateReservoir(SimpleFixedSizeExemplarReservoir):
    """Keep the pinned SDK's uniform reservoir algorithm and exemplar storage."""

    def __init__(self, size: int = 1, private_random: PrivateRandom | None = None, **kwargs) -> None:
        super().__init__(size=size, **kwargs)
        self.random = private_random if private_random is not None else PrivateRandom()

    def _find_bucket_index(self, value, time_unix_nano, attributes, context) -> int:
        # The SDK calls this under its reservoir lock and resets the count on collect.
        self._measurements_seen += 1
        if self._measurements_seen <= self._size:
            return self._measurements_seen - 1
        index = self.random.below(self._measurements_seen)
        if index < self._size:
            return index
        raise BucketIndexError('No exemplar bucket selected')


def metric_views(boundaries: list[float]) -> list[View]:
    """Cover owned instrument types once, preserving explicit histogram buckets."""
    # Share one generator per provider rather than one MT state per attribute set.
    generator = PrivateRandom()
    reservoir = partial(PrivateReservoir, private_random=generator)
    views = [View(instrument_type=kind, exemplar_reservoir_factory=lambda _: reservoir)
             for kind in (Counter, ObservableCounter, ObservableGauge)]
    views.append(View(instrument_type=Histogram,
                      aggregation=ExplicitBucketHistogramAggregation(boundaries)))
    return views
