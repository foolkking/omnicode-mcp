"""Prometheus-compatible metrics — counters and histograms.

We intentionally do NOT depend on the ``prometheus_client`` package.
The Prometheus text format is dead-simple and the dependency would
drag a thread + asyncio integration story into core. This is ~80
lines of plain Python.

Usage::

    from omnicode_core.observability.metrics import get_metrics_registry

    metrics = get_metrics_registry()
    metrics.inc("patch_apply_total", labels={"outcome": "ok"})
    with metrics.timer("search_text_seconds", labels={"mode": "auto"}):
        ...

The renderer at :meth:`MetricsRegistry.render_prometheus` produces a
text string suitable for ``GET /monitoring/metrics?format=prometheus``.

Failure mode is a no-op: if the registry runs out of memory or the
counters table grows pathologically (≥ 100 k unique label sets) we
silently drop new label sets to protect the server.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Hard cap on unique label sets per metric name to prevent runaway
# cardinality eating the heap.
_MAX_LABEL_SETS_PER_METRIC = 1000


def _label_key(labels: Optional[Dict[str, str]]) -> Tuple[Tuple[str, str], ...]:
    """Stable, hashable representation of a label dict."""
    if not labels:
        return ()
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


class MetricsRegistry:
    """Process-wide registry for counters and histograms.

    Thread-safe via a single lock — fine for our throughput (a few
    thousand requests/sec at most). If we ever need lock-free counters
    we can swap in atomics.
    """

    # Default histogram buckets (seconds). Covers fast (sub-ms) and slow
    # (multi-second) end. Sums + counts are also tracked so quantiles
    # can be approximated client-side.
    _DEFAULT_BUCKETS = (
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
        1.0, 2.5, 5.0, 10.0, 30.0,
    )

    def __init__(self) -> None:
        self._counters: Dict[str, Dict[Tuple, float]] = {}
        self._histograms: Dict[str, Dict[Tuple, "_Histogram"]] = {}
        self._lock = threading.Lock()

    def inc(
        self,
        name: str,
        amount: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Increment a counter."""
        key = _label_key(labels)
        with self._lock:
            bucket = self._counters.setdefault(name, {})
            if key not in bucket:
                if len(bucket) >= _MAX_LABEL_SETS_PER_METRIC:
                    # Cardinality blowout — drop silently.
                    return
                bucket[key] = 0.0
            bucket[key] += amount

    def observe(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Record an observation (e.g. latency in seconds)."""
        key = _label_key(labels)
        with self._lock:
            bucket = self._histograms.setdefault(name, {})
            if key not in bucket:
                if len(bucket) >= _MAX_LABEL_SETS_PER_METRIC:
                    return
                bucket[key] = _Histogram(self._DEFAULT_BUCKETS)
            bucket[key].observe(value)

    @contextmanager
    def timer(
        self,
        name: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> Iterator[None]:
        """Context manager that records elapsed wall-clock time."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - start, labels)

    def render_prometheus(self) -> str:
        """Render the registry as a Prometheus text-format string."""
        lines: List[str] = []
        with self._lock:
            for name, bucket in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                for label_tuple, value in bucket.items():
                    lbl = _format_labels(label_tuple)
                    lines.append(f"{name}{lbl} {value}")
            for name, bucket in sorted(self._histograms.items()):
                lines.append(f"# TYPE {name} histogram")
                for label_tuple, hist in bucket.items():
                    base_labels = list(label_tuple)
                    for upper, count in hist.cumulative_buckets():
                        lbl_with_le = base_labels + [("le", _fmt_le(upper))]
                        lines.append(
                            f"{name}_bucket{_format_labels(tuple(lbl_with_le))} {count}"
                        )
                    lbl_with_le = base_labels + [("le", "+Inf")]
                    lines.append(
                        f"{name}_bucket{_format_labels(tuple(lbl_with_le))} {hist.count}"
                    )
                    lbl = _format_labels(label_tuple)
                    lines.append(f"{name}_sum{lbl} {hist.sum}")
                    lines.append(f"{name}_count{lbl} {hist.count}")
        return "\n".join(lines) + "\n"

    def render_json(self) -> Dict:
        """Render as a JSON-serialisable dict (for health endpoints)."""
        out: Dict[str, dict] = {"counters": {}, "histograms": {}}
        with self._lock:
            for name, bucket in self._counters.items():
                out["counters"][name] = [
                    {"labels": dict(k), "value": v}
                    for k, v in bucket.items()
                ]
            for name, bucket in self._histograms.items():
                out["histograms"][name] = [
                    {
                        "labels": dict(k),
                        "count": hist.count,
                        "sum": hist.sum,
                        "buckets": dict(hist.cumulative_buckets()),
                    }
                    for k, hist in bucket.items()
                ]
        return out


def _format_labels(label_tuple: Tuple[Tuple[str, str], ...]) -> str:
    if not label_tuple:
        return ""
    parts = [f'{k}="{_escape(v)}"' for k, v in label_tuple]
    return "{" + ",".join(parts) + "}"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_le(upper: float) -> str:
    if upper.is_integer():
        return f"{upper:.0f}"
    return f"{upper}"


class _Histogram:
    __slots__ = ("buckets", "counts", "count", "sum")

    def __init__(self, buckets: Tuple[float, ...]) -> None:
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.count = 0
        self.sum = 0.0

    def observe(self, value: float) -> None:
        self.count += 1
        self.sum += value
        for i, upper in enumerate(self.buckets):
            if value <= upper:
                self.counts[i] += 1

    def cumulative_buckets(self) -> Iterator[Tuple[float, int]]:
        running = 0
        for upper, c in zip(self.buckets, self.counts, strict=False):
            running += c
            yield upper, running


_DEFAULT: Optional[MetricsRegistry] = None


def get_metrics_registry() -> MetricsRegistry:
    """Process-wide metrics registry singleton."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = MetricsRegistry()
    return _DEFAULT


__all__ = ["MetricsRegistry", "get_metrics_registry"]
