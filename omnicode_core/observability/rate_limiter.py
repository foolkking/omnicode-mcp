"""Per-IP per-endpoint token-bucket rate limiter.

Used to gate ``/admin/*`` traffic so a misbehaving client (or a
brute-force attempt against the bootstrap token issue path) can't
flood the server. Every (ip, endpoint) tuple gets its own bucket;
buckets refill at a steady rate up to a small maximum.

We deliberately keep this in-process (no Redis dependency). The
single-tenant self-host shape we target makes that fine. If you ever
front this with multiple replicas, a real distributed rate limiter
would be a P3 swap.

Default policy:

* ``/admin/*`` writes: 30 requests / minute / IP (burst of 10).
* Everything else: not rate-limited (callers compose with nginx
  limit_req for general DoS protection).

The limiter is instantiated lazily and shared across requests via
``get_rate_limiter()``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


@dataclass
class _Bucket:
    tokens: float
    last_refill_ts: float


class RateLimiter:
    """Simple token bucket keyed by ``(scope, identity)``.

    ``scope`` is a stable string like ``"admin"`` or ``"patch_apply"``;
    ``identity`` is the IP address (or username when authenticated).
    """

    def __init__(
        self,
        *,
        rate_per_minute: float = 30.0,
        burst: int = 10,
    ) -> None:
        self.rate_per_second = rate_per_minute / 60.0
        self.burst = burst
        self._buckets: Dict[Tuple[str, str], _Bucket] = {}
        self._lock = threading.Lock()
        # Cap distinct (scope, identity) tuples to prevent memory blowout
        # under attack.
        self._cap = 50_000

    def check(
        self,
        scope: str,
        identity: str,
        *,
        cost: float = 1.0,
    ) -> Tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)``.

        On allow, ``retry_after_seconds == 0``. On deny, ``retry_after_seconds``
        is how long the caller should wait before the next attempt would
        succeed.
        """
        if not identity:
            identity = "anonymous"
        now = time.monotonic()
        key = (scope, identity)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._cap:
                    # Drop the oldest bucket — under attack we'd rather
                    # retain newer hosts than older.
                    self._buckets.pop(next(iter(self._buckets)))
                bucket = _Bucket(tokens=float(self.burst), last_refill_ts=now)
                self._buckets[key] = bucket
            else:
                # Refill
                elapsed = now - bucket.last_refill_ts
                bucket.tokens = min(
                    float(self.burst),
                    bucket.tokens + elapsed * self.rate_per_second,
                )
                bucket.last_refill_ts = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0
            # Not enough tokens
            deficit = cost - bucket.tokens
            retry_after = deficit / max(self.rate_per_second, 1e-9)
            return False, retry_after


_DEFAULT: dict = {}


def get_rate_limiter(
    scope: str,
    *,
    rate_per_minute: float = 30.0,
    burst: int = 10,
) -> RateLimiter:
    """Return a process-wide rate limiter for the given scope.

    Different scopes get different limiters so policies can be tuned
    independently. The first call for a scope sets the policy; later
    calls reuse the same limiter (the per-scope kwargs are ignored).
    """
    if scope not in _DEFAULT:
        _DEFAULT[scope] = RateLimiter(
            rate_per_minute=rate_per_minute, burst=burst,
        )
    return _DEFAULT[scope]


__all__ = ["RateLimiter", "get_rate_limiter"]
