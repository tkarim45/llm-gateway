"""HTTP surface (FastAPI).

  POST /v1/chat            — {prompt, max_tokens?, stream?} → completion (SSE when stream=true)
  GET  /v1/usage           — the calling tenant's metered usage + quota state
  GET  /v1/health          — provider chain health (which upstream is serving, failure counts)
  GET  /v1/audit           — last N audit records (admin visibility)
  GET  /healthz            — liveness

Request flow: API key → tenant → rate limit (429) → quota (402) → failover chain → meter + audit.
"""
from __future__ import annotations

import json
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .audit import Audit
from .config import Config, Tenant, load_tenants
from .failover import Failover
from .metering import Meter
from .providers import ProviderError, build_chain
from .ratelimit import QuotaTracker, RateLimiter


class ChatRequest(BaseModel):
    prompt: str
    max_tokens: int = 256
    stream: bool = False


def create_app(cfg: Config | None = None, chain: list | None = None,
               tenants: dict[str, Tenant] | None = None) -> FastAPI:
    cfg = cfg or Config()
    tenants = tenants or load_tenants()
    failover = Failover(chain if chain is not None else build_chain(cfg))
    limiter = RateLimiter()
    quotas = QuotaTracker()
    meter = Meter(cfg)
    audit = Audit(cfg.audit_path)

    app = FastAPI(title="llm-gateway")
    app.state.failover = failover

    def tenant_of(authorization: str = Header(default="")) -> Tenant:
        key = authorization.removeprefix("Bearer ").strip()
        t = tenants.get(key)
        if t is None:
            raise HTTPException(401, "invalid API key")
        return t

    def gate(t: Tenant) -> None:
        allowed, retry = limiter.allow(t)
        if not allowed:
            raise HTTPException(429, "rate limit exceeded",
                                headers={"Retry-After": f"{retry:.2f}"})
        if quotas.would_exceed(t):
            raise HTTPException(402, f"token quota exhausted ({t.token_quota})")

    @app.post("/v1/chat")
    def chat(req: ChatRequest, t: Tenant = Depends(tenant_of)):
        gate(t)
        t0 = time.perf_counter()

        if req.stream:
            def sse():
                served_by = None
                try:
                    for kind, item in failover.stream(req.prompt, req.max_tokens):
                        if kind == "provider":
                            served_by = item
                            yield f'event: provider\ndata: {item}\n\n'
                        elif kind == "chunk":
                            yield f"data: {json.dumps(item)}\n\n"
                        else:  # usage sentinel
                            tokens = item.tokens_in + item.tokens_out
                            quotas.add(t.tenant_id, tokens)
                            cost = meter.record(t.tenant_id, item.tokens_in, item.tokens_out)
                            audit.record(tenant=t.tenant_id, provider=served_by, stream=True,
                                         status=200, tokens_in=item.tokens_in,
                                         tokens_out=item.tokens_out, cost_usd=round(cost, 6),
                                         latency_ms=round((time.perf_counter()-t0)*1000, 1))
                            yield f'event: usage\ndata: {json.dumps({"tokens_in": item.tokens_in, "tokens_out": item.tokens_out})}\n\n'
                    yield "event: done\ndata: [DONE]\n\n"
                except ProviderError as e:
                    audit.record(tenant=t.tenant_id, provider=None, stream=True, status=502,
                                 error=str(e)[:200])
                    yield f'event: error\ndata: {json.dumps(str(e))}\n\n'
            return StreamingResponse(sse(), media_type="text/event-stream")

        try:
            text, usage, provider, attempts, up_ms = failover.complete(req.prompt, req.max_tokens)
        except ProviderError as e:
            audit.record(tenant=t.tenant_id, provider=None, status=502, error=str(e)[:200])
            raise HTTPException(502, str(e))
        tokens = usage.tokens_in + usage.tokens_out
        quotas.add(t.tenant_id, tokens)
        cost = meter.record(t.tenant_id, usage.tokens_in, usage.tokens_out)
        latency_ms = (time.perf_counter() - t0) * 1000
        rid = audit.record(tenant=t.tenant_id, provider=provider, status=200,
                           attempts=attempts, tokens_in=usage.tokens_in,
                           tokens_out=usage.tokens_out, cost_usd=round(cost, 6),
                           latency_ms=round(latency_ms, 1))
        return {"request_id": rid, "text": text, "provider": provider, "attempts": attempts,
                "usage": {"tokens_in": usage.tokens_in, "tokens_out": usage.tokens_out,
                          "cost_usd": round(cost, 6)},
                "latency_ms": round(latency_ms, 1)}

    @app.get("/v1/usage")
    def usage(t: Tenant = Depends(tenant_of)):
        rep = meter.report(t.tenant_id)[t.tenant_id]
        used = quotas.used(t.tenant_id)
        return rep | {"quota": t.token_quota, "quota_used": used,
                      "quota_remaining": max(0, t.token_quota - used)}

    @app.get("/v1/health")
    def health():
        return failover.health()

    @app.get("/v1/audit")
    def audit_tail(n: int = 20, t: Tenant = Depends(tenant_of)):
        return [r for r in audit.tail(n) if r.get("tenant") == t.tenant_id]

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


app = create_app()
