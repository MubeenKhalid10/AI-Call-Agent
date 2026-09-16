"""A metrics registry with no dependencies: counters, gauges, histograms. Phase 22.

Every process here — the bot, the scheduler, the dashboard, the automation
API, the webhook receiver — keeps its own numbers in one of these and serves
them at `/metrics` in the Prometheus text format, and at `/metrics.json` as
plain data. Nothing is installed for it: the exposition format is a few
lines of text per metric, and a dependency for that would be a second thing
to keep current for the sake of a formatter.

**What a metric is, here.** A *counter* only goes up (calls placed, errors
seen). A *gauge* is a level (calls in flight, queue depth). A *histogram*
records observations of a duration, a size or a cost and keeps two views of
them: Prometheus's cumulative buckets, so `histogram_quantile` works across
processes, and a bounded reservoir of the most recent samples, so the JSON
snapshot can say p50, p95, p99 and the mean exactly without a Prometheus in
front of it. Both views are written from the same observation.

**Labels never carry a person.** A label value is what the metric is *about*
— a stage, a provider, an outcome, a campaign *id* — and never a phone
number, a name, an email or a transcript. `LABEL_NAMES_ALLOWED` is the list;
a label outside it is refused at definition time, which is how a future
`phone=` label fails a check instead of reaching a scrape. Cardinality is
the other reason: every distinct label value is a series kept for the life
of the process, so a label that can take a million values is a memory leak
with a dashboard.

**Process-local, by design.** Counters start at zero when the process does,
which is what Prometheus expects (`rate()` handles the resets). Numbers that
must be true across the fleet — queue depth, who is alive, throughput —
come from PostgreSQL through `collect.py`, refreshed into gauges here, so a
scrape of any one process answers for the deployment.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

#: Every label name any metric may use. Deliberately short and closed: a
#: label is an axis a dashboard groups by, and none of these can identify a
#: person. Adding one is a code change that a reader will see.
LABEL_NAMES_ALLOWED = frozenset(
    {
        "campaign",  # a campaign *id*, never its name
        "stage",  # stt / llm / tts / total / turn_end
        "provider",  # a vendor's name: twilio, signalwire, hubspot, calcom
        "outcome",  # placed / refused / synced / failed / ...
        "status",  # a call attempt's final status, a worker's state
        "kind",  # an event kind, an error family, a token kind
        "operation",  # a store or tool operation's name
        "model",  # an LLM / TTS / STT model id
        "transport",  # webrtc / twilio / eval / ...
        "reason",  # why a session ended
        "role",  # which process: bot / scheduler / api / ...
        "method",  # an HTTP method
        "route",  # an HTTP route *template*, never the raw path
        "bucket",  # a queue-depth bucket
        "state",  # a worker health state
    }
)

#: A metric name must look like this, so the exposition parser accepts it.
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:")

#: How many recent observations a histogram keeps for exact percentiles.
DEFAULT_RESERVOIR = 1024

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

LabelKey = tuple[tuple[str, str], ...]


class MetricError(ValueError):
    """A metric or a label was defined or used wrongly. Raised at definition or use, never at scrape."""


def _check_name(name: str) -> None:
    if not name or not set(name) <= _NAME_CHARS or name[0].isdigit():
        raise MetricError(f"{name!r} is not a valid metric name")


def _check_labels(name: str, labels: Iterable[str]) -> tuple[str, ...]:
    found = tuple(labels)
    for label in found:
        if label not in LABEL_NAMES_ALLOWED:
            raise MetricError(
                f"metric {name!r} uses the label {label!r}, which is not in LABEL_NAMES_ALLOWED — "
                f"labels must never carry a person, and every new axis is a code change"
            )
    if len(set(found)) != len(found):
        raise MetricError(f"metric {name!r} repeats a label")
    return found


def _label_key(declared: tuple[str, ...], given: dict[str, Any], name: str) -> LabelKey:
    """The label values as a hashable key, in the declared order, every one a string."""
    if set(given) != set(declared):
        raise MetricError(
            f"metric {name!r} takes labels {list(declared)}; got {sorted(given)}"
        )
    return tuple((label, _label_value(given[label])) for label in declared)


def _label_value(value: Any) -> str:
    """A label value as text. `None` reads as `none`, an enum as its value."""
    if value is None:
        return "none"
    raw = getattr(value, "value", value)
    text = str(raw)
    # A value is bounded on purpose: a label is an axis, not a message.
    return text if len(text) <= 80 else text[:77] + "..."


class _Metric:
    """What every metric shares: a name, a help line, declared labels, a lock."""

    kind = "untyped"

    def __init__(self, name: str, help: str, labels: Iterable[str] = ()) -> None:
        _check_name(name)
        self.name = name
        self.help = " ".join(help.split())
        self.labels = _check_labels(name, labels)
        self._lock = threading.Lock()

    def describe(self) -> dict[str, Any]:
        """The definition, for the JSON snapshot."""
        return {"name": self.name, "type": self.kind, "help": self.help, "labels": list(self.labels)}


class Counter(_Metric):
    """A number that only goes up."""

    kind = "counter"

    def __init__(self, name: str, help: str, labels: Iterable[str] = ()) -> None:
        super().__init__(name, help, labels)
        self._values: dict[LabelKey, float] = {}

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        """Add `amount` (default one) to the series these labels name."""
        if amount < 0:
            raise MetricError(f"counter {self.name!r} cannot go down")
        key = _label_key(self.labels, labels, self.name)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: Any) -> float:
        """The series' current total (zero when never incremented)."""
        key = _label_key(self.labels, labels, self.name)
        with self._lock:
            return self._values.get(key, 0.0)

    def total(self) -> float:
        """Every series summed."""
        with self._lock:
            return sum(self._values.values())

    def series(self) -> list[tuple[LabelKey, float]]:
        with self._lock:
            return sorted(self._values.items())


class Gauge(_Metric):
    """A level that goes up and down."""

    kind = "gauge"

    def __init__(self, name: str, help: str, labels: Iterable[str] = ()) -> None:
        super().__init__(name, help, labels)
        self._values: dict[LabelKey, float] = {}

    def set(self, value: float, **labels: Any) -> None:
        """Set the series to `value`."""
        key = _label_key(self.labels, labels, self.name)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        """Move the series up by `amount`."""
        key = _label_key(self.labels, labels, self.name)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: Any) -> None:
        """Move the series down by `amount`, never below zero — a level cannot owe."""
        key = _label_key(self.labels, labels, self.name)
        with self._lock:
            self._values[key] = max(0.0, self._values.get(key, 0.0) - amount)

    def value(self, **labels: Any) -> float:
        key = _label_key(self.labels, labels, self.name)
        with self._lock:
            return self._values.get(key, 0.0)

    def clear(self) -> None:
        """Forget every series — for a refresh that replaces the whole picture."""
        with self._lock:
            self._values.clear()

    def series(self) -> list[tuple[LabelKey, float]]:
        with self._lock:
            return sorted(self._values.items())


@dataclass
class _HistogramSeries:
    """One labelled series of a histogram: buckets, sum, count, and the reservoir."""

    buckets: list[int]
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0
    recent: deque = field(default_factory=deque)


@dataclass(frozen=True)
class HistogramStats:
    """The exact figures over the recent reservoir, plus the lifetime count and sum."""

    count: int
    sum: float
    mean: float | None
    p50: float | None
    p95: float | None
    p99: float | None
    max: float | None
    recent: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "sum": round(self.sum, 6),
            "mean": _round(self.mean),
            "p50": _round(self.p50),
            "p95": _round(self.p95),
            "p99": _round(self.p99),
            "max": _round(self.max),
            "recent": self.recent,
        }


class Histogram(_Metric):
    """Observations of a duration, a size or a cost.

    `buckets` are upper bounds in the unit observed (seconds for a duration).
    They are cumulative in the exposition, as Prometheus wants; the JSON
    snapshot reports percentiles from the reservoir instead.
    """

    kind = "histogram"

    def __init__(
        self,
        name: str,
        help: str,
        labels: Iterable[str] = (),
        *,
        buckets: Iterable[float] = DEFAULT_BUCKETS,
        reservoir: int = DEFAULT_RESERVOIR,
    ) -> None:
        super().__init__(name, help, labels)
        bounds = sorted(float(b) for b in buckets)
        if not bounds or any(b <= 0 for b in bounds) or len(set(bounds)) != len(bounds):
            raise MetricError(f"histogram {self.name!r} needs distinct positive bucket bounds")
        self.bounds = tuple(bounds)
        self._reservoir = max(16, int(reservoir))
        self._series: dict[LabelKey, _HistogramSeries] = {}

    def observe(self, value: float, **labels: Any) -> None:
        """Record one observation. Negative and non-finite values are ignored, not raised: a bad timer must not break a call."""
        if value is None or not math.isfinite(value) or value < 0:
            return
        key = _label_key(self.labels, labels, self.name)
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = _HistogramSeries(buckets=[0] * len(self.bounds), recent=deque(maxlen=self._reservoir))
                self._series[key] = series
            for index, bound in enumerate(self.bounds):
                if value <= bound:
                    series.buckets[index] += 1
            series.count += 1
            series.total += value
            series.maximum = max(series.maximum, value)
            series.recent.append(value)

    def stats(self, **labels: Any) -> HistogramStats:
        """Exact percentiles over the reservoir for one series."""
        key = _label_key(self.labels, labels, self.name)
        with self._lock:
            series = self._series.get(key)
            if series is None:
                return HistogramStats(0, 0.0, None, None, None, None, None, 0)
            return _stats(series)

    def series(self) -> list[tuple[LabelKey, _HistogramSeries]]:
        with self._lock:
            return sorted(self._series.items())

    def snapshot_series(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"labels": dict(key), **_stats(series).to_dict()}
                for key, series in sorted(self._series.items())
            ]


def _stats(series: _HistogramSeries) -> HistogramStats:
    ordered = sorted(series.recent)
    if not ordered:
        return HistogramStats(series.count, series.total, None, None, None, None, None, 0)
    return HistogramStats(
        count=series.count,
        sum=series.total,
        mean=sum(ordered) / len(ordered),
        p50=percentile(ordered, 0.50),
        p95=percentile(ordered, 0.95),
        p99=percentile(ordered, 0.99),
        max=series.maximum,
        recent=len(ordered),
    )


def percentile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted list. The same rule `metrics.py` uses."""
    if not ordered:
        raise ValueError("no samples")
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


class MetricsRegistry:
    """Every metric a process has, in definition order."""

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()
        self.created_at = time.time()

    def counter(self, name: str, help: str, labels: Iterable[str] = ()) -> Counter:
        """Define a counter, or return the one already defined under this name."""
        return self._define(Counter, name, help, labels)

    def gauge(self, name: str, help: str, labels: Iterable[str] = ()) -> Gauge:
        return self._define(Gauge, name, help, labels)

    def histogram(
        self,
        name: str,
        help: str,
        labels: Iterable[str] = (),
        *,
        buckets: Iterable[float] = DEFAULT_BUCKETS,
        reservoir: int = DEFAULT_RESERVOIR,
    ) -> Histogram:
        return self._define(Histogram, name, help, labels, buckets=buckets, reservoir=reservoir)

    def _define(self, cls: type, name: str, help: str, labels: Iterable[str], **kwargs: Any) -> Any:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, cls) or existing.labels != tuple(labels):
                    raise MetricError(f"metric {name!r} is already defined with a different type or labels")
                return existing
            metric = cls(name, help, labels, **kwargs)
            self._metrics[name] = metric
            return metric

    def get(self, name: str) -> _Metric | None:
        with self._lock:
            return self._metrics.get(name)

    def __iter__(self) -> Iterator[_Metric]:
        with self._lock:
            return iter(list(self._metrics.values()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._metrics)

    def reset(self) -> None:
        """Forget every value but keep every definition. For the checks."""
        for metric in self:
            if isinstance(metric, Counter | Gauge):
                with metric._lock:
                    metric._values.clear()
            elif isinstance(metric, Histogram):
                with metric._lock:
                    metric._series.clear()

    # --- Rendering ---------------------------------------------------------------

    def render_prometheus(self) -> str:
        """The registry in the Prometheus text exposition format (version 0.0.4)."""
        lines: list[str] = []
        for metric in self:
            lines.append(f"# HELP {metric.name} {_escape_help(metric.help)}")
            lines.append(f"# TYPE {metric.name} {metric.kind}")
            if isinstance(metric, Counter | Gauge):
                for key, value in metric.series():
                    lines.append(f"{metric.name}{_labels(key)} {_number(value)}")
            elif isinstance(metric, Histogram):
                for key, series in metric.series():
                    for bound, count in zip(metric.bounds, series.buckets, strict=True):
                        lines.append(
                            f"{metric.name}_bucket{_labels(key, le=_number(bound))} {count}"
                        )
                    lines.append(f"{metric.name}_bucket{_labels(key, le='+Inf')} {series.count}")
                    lines.append(f"{metric.name}_sum{_labels(key)} {_number(series.total)}")
                    lines.append(f"{metric.name}_count{_labels(key)} {series.count}")
        return "\n".join(lines) + "\n"

    def snapshot(self) -> dict[str, Any]:
        """The registry as plain data: every series, and exact percentiles for histograms."""
        out: dict[str, Any] = {}
        for metric in self:
            entry = metric.describe()
            if isinstance(metric, Counter | Gauge):
                entry["series"] = [{"labels": dict(key), "value": value} for key, value in metric.series()]
            elif isinstance(metric, Histogram):
                entry["buckets"] = list(metric.bounds)
                entry["series"] = metric.snapshot_series()
            out[metric.name] = entry
        return out


def _labels(key: LabelKey, **extra: str) -> str:
    parts = [f'{name}="{_escape_value(value)}"' for name, value in key]
    parts.extend(f'{name}="{_escape_value(value)}"' for name, value in extra.items())
    return "{" + ",".join(parts) + "}" if parts else ""


def _escape_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _number(value: float) -> str:
    """A float the way Prometheus reads it: integers without a decimal point."""
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


#: The process's registry. Every instrument in `instruments.py` is defined on it.
REGISTRY = MetricsRegistry()


__all__ = [
    "DEFAULT_BUCKETS",
    "LABEL_NAMES_ALLOWED",
    "REGISTRY",
    "Counter",
    "Gauge",
    "Histogram",
    "HistogramStats",
    "MetricError",
    "MetricsRegistry",
    "percentile",
]
