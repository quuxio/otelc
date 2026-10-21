"""Strict, small common policy shared by every language backend."""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LANGUAGES = ("java", "typescript", "javascript", "python", "go", "c", "cpp", "rust")
SECTIONS = {
    "resource": {"service_name"},
    "export": {"endpoint", "interval_ms", "timeout_ms"},
    "sources": {"include", "exclude"},
    "functions": {"include", "exclude"},
    "metrics": {"collection", "emission", "histogram_boundaries_seconds"},
    "traces": {"enabled", "sample_ratio"},
    "lifetimes": {"enabled", "include", "exclude", "max_live", "release_methods"},
    "benchmark": {"iterations", "samples", "warmup"},
}


def patterns(value: object, label: str) -> tuple[str, ...]:
    """Validate user selections without interpreting punctuation as regex."""
    if not isinstance(value, list) or len(value) > 4096:
        raise ValueError(f"{label} must be a list with at most 4096 entries")
    if any(not isinstance(item, str) or not item or len(item) > 4096 for item in value):
        raise ValueError(f"{label} entries must be non-empty strings of at most 4096 characters")
    return tuple(value)


def expression(pattern: str, *, path: bool = False) -> str:
    """Compile * and ? name globs, and slash-aware source globs."""
    result = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if path and pattern[index:index + 3] == "**/":
            result += "(?:.*/)?"
            index += 3
            continue
        if path and pattern[index:index + 2] == "**":
            result += ".*"
            index += 2
            continue
        wildcards = {"*": "[^/]*", "?": "[^/]"} if path else {"*": ".*", "?": "."}
        result += wildcards.get(char, re.escape(char))
        index += 1
    return "^" + result + "$"


@dataclass(frozen=True)
class Selection:
    """An explicit opt-in set with exclusion precedence."""

    include: tuple[str, ...]
    exclude: tuple[str, ...]
    paths: bool = False

    def accepts(self, name: str) -> bool:
        """Select a name using the authoritative common glob implementation."""
        def matches(items: tuple[str, ...]) -> bool:
            return any(re.fullmatch(expression(item, path=self.paths), name, re.DOTALL) for item in items)

        return matches(self.include) and not matches(self.exclude)


def validate_selectors_and_flags(data: dict[str, Any]) -> None:
    """Validate selections and switches shared by every backend."""
    for section in ("sources", "functions", "lifetimes"):
        for key in ("include", "exclude"):
            patterns(data[section][key], f"{section}.{key}")
    patterns(data["lifetimes"]["release_methods"], "lifetimes.release_methods")
    flags = (("metrics", "collection"), ("metrics", "emission"),
             ("traces", "enabled"), ("lifetimes", "enabled"))
    for section, key in flags:
        if type(data[section][key]) is not bool:
            raise ValueError(f"{section}.{key} must be boolean")


def validate_limits(data: dict[str, Any]) -> None:
    """Bound intervals, benchmark work and lifecycle bookkeeping."""
    limits = (("export", ("interval_ms", "timeout_ms")),
              ("benchmark", ("iterations", "samples", "warmup")),
              ("lifetimes", ("max_live",)))
    for section, keys in limits:
        for key in keys:
            value = data[section][key]
            if type(value) is not int or not 1 <= value <= 1_000_000:
                raise ValueError(f"{section}.{key} must be an integer in 1..1000000")


def validate_destination(data: dict[str, Any]) -> None:
    """Reject malformed resource names and credential-bearing export URLs."""
    name = data["resource"]["service_name"]
    if not isinstance(name, str) or not name or len(name) > 128 or any(ord(c) < 32 for c in name):
        raise ValueError("resource.service_name must be a bounded non-empty name")
    endpoint = data["export"]["endpoint"]
    if not isinstance(endpoint, str):
        raise ValueError("export.endpoint must be a URL")
    url = urlsplit(endpoint)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("export.endpoint must be an HTTP(S) base URL without credentials/query/fragment")


def validate_distributions(data: dict[str, Any]) -> None:
    """Check trace sampling and increasing positive histogram boundaries."""
    ratio = data["traces"]["sample_ratio"]
    if type(ratio) not in (int, float) or not math.isfinite(ratio) or not 0 <= ratio <= 1:
        raise ValueError("traces.sample_ratio must be finite in 0..1")
    buckets = data["metrics"]["histogram_boundaries_seconds"]
    if not isinstance(buckets, list) or not buckets or len(buckets) > 128:
        raise ValueError("histogram boundaries must be a non-empty bounded list")
    previous = 0.
    for value in buckets:
        if type(value) not in (int, float) or not math.isfinite(value) or value <= previous:
            raise ValueError("histogram boundaries must be finite, positive and increasing")
        previous = value


@dataclass(frozen=True)
class Policy:
    """Validated user intent; no language/tool-specific settings are required."""

    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> Policy:
        """Read TOML and reject unsupported or misspelt options before launch."""
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        return cls.parse(data)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Policy:
        """Apply defaults once and validate every common field."""
        unknown = set(data) - {"schema_version", "languages", *SECTIONS}
        if unknown or type(data.get("schema_version")) is not int or data["schema_version"] != 3:
            raise ValueError(f"expected schema_version=3 and known fields; unknown={sorted(unknown)}")
        languages = patterns(data.get("languages", list(LANGUAGES)), "languages")
        if not languages or len(set(languages)) != len(languages) or set(languages) - set(LANGUAGES):
            raise ValueError("languages must contain unique supported language IDs")
        defaults: dict[str, dict[str, Any]] = {
            "resource": {"service_name": "otelc-example"},
            "export": {"endpoint": "http://127.0.0.1:4320", "interval_ms": 1000, "timeout_ms": 1000},
            "sources": {"include": [], "exclude": []},
            "functions": {"include": [], "exclude": []},
            "metrics": {"collection": True, "emission": True, "histogram_boundaries_seconds": [1e-6, 1e-5, 1e-4, .001, .01, .1, 1.]},
            "traces": {"enabled": False, "sample_ratio": .01},
            "lifetimes": {"enabled": False, "include": [], "exclude": [], "max_live": 4096, "release_methods": ["close"]},
            "benchmark": {"iterations": 10000, "samples": 15, "warmup": 5},
        }
        resolved: dict[str, Any] = {"schema_version": 3, "languages": list(languages)}
        for section, fields in SECTIONS.items():
            supplied = data.get(section, {})
            if not isinstance(supplied, dict) or set(supplied) - fields:
                raise ValueError(f"unknown or invalid {section} settings")
            resolved[section] = defaults[section] | supplied
        validate_selectors_and_flags(resolved)
        validate_limits(resolved)
        validate_destination(resolved)
        validate_distributions(resolved)
        return cls(resolved)

    def selection(self, section: str) -> Selection:
        """Return a validated selector for application sources/functions/types."""
        return Selection(tuple(self.data[section]["include"]), tuple(self.data[section]["exclude"]), section == "sources")
