"""Per-tenant cost metering: tokens in/out and USD cost per request, aggregated into the usage
report a platform team bills from."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .config import Config


@dataclass
class TenantUsage:
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class Meter:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._usage: dict[str, TenantUsage] = {}

    def record(self, tenant_id: str, tokens_in: int, tokens_out: int) -> float:
        cost = (tokens_in * self.cfg.price_in_per_1m
                + tokens_out * self.cfg.price_out_per_1m) / 1_000_000
        with self._lock:
            u = self._usage.setdefault(tenant_id, TenantUsage())
            u.requests += 1
            u.tokens_in += tokens_in
            u.tokens_out += tokens_out
            u.cost_usd += cost
        return cost

    def report(self, tenant_id: str | None = None) -> dict:
        with self._lock:
            items = ({tenant_id: self._usage.get(tenant_id, TenantUsage())}
                     if tenant_id else dict(self._usage))
            return {tid: {"requests": u.requests, "tokens_in": u.tokens_in,
                          "tokens_out": u.tokens_out, "cost_usd": round(u.cost_usd, 6)}
                    for tid, u in items.items()}
