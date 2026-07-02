"""Gateway configuration: tenants (API key, rate limit, token quota) and the provider chain.

Tenants load from GATEWAY_TENANTS_FILE (JSON) if set, else two demo tenants ship so the gateway
runs out of the box. API keys here are demo values — real deployments inject them via the file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(os.path.expanduser("~/.env"))
except Exception:  # pragma: no cover
    pass


@dataclass
class Tenant:
    tenant_id: str
    api_key: str
    rate_limit_rps: float = 5.0          # sustained requests/second (token bucket)
    burst: int = 10                      # bucket capacity
    token_quota: int = 200_000           # total tokens (in+out) before 402


@dataclass
class Config:
    providers: list = field(default_factory=lambda: [
        p.strip() for p in os.getenv("GATEWAY_PROVIDERS", "bedrock,anthropic").split(",") if p.strip()
    ])
    provider_timeout_s: float = float(os.getenv("GATEWAY_PROVIDER_TIMEOUT_S", 30))
    bedrock_model: str = os.getenv(
        "GATEWAY_BEDROCK_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
    anthropic_model: str = os.getenv("GATEWAY_ANTHROPIC_MODEL", "claude-haiku-4-5")
    aws_region: str = os.getenv("AWS_REGION", "us-east-1")
    audit_path: str = os.getenv("GATEWAY_AUDIT_PATH", "audit.log.jsonl")

    # blended USD per 1M tokens for cost metering (haiku-class)
    price_in_per_1m: float = float(os.getenv("GATEWAY_PRICE_IN", 1.0))
    price_out_per_1m: float = float(os.getenv("GATEWAY_PRICE_OUT", 5.0))


def load_tenants() -> dict[str, Tenant]:
    """api_key -> Tenant."""
    path = os.getenv("GATEWAY_TENANTS_FILE")
    if path and os.path.exists(path):
        raw = json.load(open(path))
        tenants = [Tenant(**t) for t in raw]
    else:
        tenants = [
            Tenant("acme", "gw-acme-demo-key", rate_limit_rps=5, burst=10, token_quota=200_000),
            Tenant("globex", "gw-globex-demo-key", rate_limit_rps=1, burst=3, token_quota=2_000),
        ]
    return {t.api_key: t for t in tenants}
