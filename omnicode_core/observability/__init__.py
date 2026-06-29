"""Observability primitives — audit log, metrics, rate limiter, idempotency.

These are 1.1 polish items lifted out of the architecture audit's
"Known limitations to fix" list:

* :mod:`audit_log`     — append-only audit log for /admin/* mutations
* :mod:`metrics`       — Prometheus-compatible counters/histograms
* :mod:`rate_limiter`  — per-IP per-endpoint token bucket
* :mod:`idempotency`   — Idempotency-Key cache for /patch/apply

All four are best-effort: failures (disk full, race conditions) log a
warning and degrade to no-op rather than blocking the request.
"""

from omnicode_core.observability.audit_log import AuditLog, get_audit_log
from omnicode_core.observability.idempotency import (
    IdempotencyConflict,
    IdempotencyStore,
    get_idempotency_store,
)
from omnicode_core.observability.metrics import (
    MetricsRegistry,
    get_metrics_registry,
)
from omnicode_core.observability.rate_limiter import (
    RateLimiter,
    get_rate_limiter,
)

__all__ = [
    "AuditLog",
    "get_audit_log",
    "IdempotencyConflict",
    "IdempotencyStore",
    "get_idempotency_store",
    "MetricsRegistry",
    "get_metrics_registry",
    "RateLimiter",
    "get_rate_limiter",
]
