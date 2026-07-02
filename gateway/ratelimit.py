"""Per-tenant token-bucket rate limiter + total-token quota tracking.

Token bucket: capacity `burst`, refill `rate_limit_rps` tokens/sec. A request takes one token or
is rejected with 429 + Retry-After (time until the next token). Quota: cumulative in+out tokens
per tenant; once exhausted the tenant gets 402 until the quota is raised — the "you've spent your
budget" backstop rate limiting can't provide.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .config import Tenant


@dataclass
class _Bucket:
    tokens: float
    last: float


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, tenant: Tenant) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(tenant.tenant_id)
            if b is None:
                b = _Bucket(tokens=float(tenant.burst), last=now)
                self._buckets[tenant.tenant_id] = b
            b.tokens = min(tenant.burst, b.tokens + (now - b.last) * tenant.rate_limit_rps)
            b.last = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0.0
            need = (1.0 - b.tokens) / tenant.rate_limit_rps
            return False, max(need, 0.01)


class QuotaTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._used: dict[str, int] = {}

    def used(self, tenant_id: str) -> int:
        with self._lock:
            return self._used.get(tenant_id, 0)

    def would_exceed(self, tenant: Tenant) -> bool:
        return self.used(tenant.tenant_id) >= tenant.token_quota

    def add(self, tenant_id: str, tokens: int) -> None:
        with self._lock:
            self._used[tenant_id] = self._used.get(tenant_id, 0) + int(tokens)
