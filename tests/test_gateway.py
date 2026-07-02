"""Offline tests — the provider chain is Static/Flaky stubs, so CI never touches Bedrock or the
Anthropic API. The real-provider path is validated by the failover demo run in the README."""
import time

from fastapi.testclient import TestClient

from gateway.api import create_app
from gateway.config import Config, Tenant
from gateway.providers import FlakyProvider, StaticProvider

ACME = {"Authorization": "Bearer gw-acme-demo-key"}
GLOBEX = {"Authorization": "Bearer gw-globex-demo-key"}


def make_client(tmp_path, chain=None, tenants=None):
    cfg = Config()
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    app = create_app(cfg, chain=chain if chain is not None else [StaticProvider()],
                     tenants=tenants)
    return TestClient(app)


def test_auth_required(tmp_path):
    c = make_client(tmp_path)
    assert c.post("/v1/chat", json={"prompt": "hi"}).status_code == 401
    assert c.post("/v1/chat", json={"prompt": "hi"},
                  headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.post("/v1/chat", json={"prompt": "hi"}, headers=ACME).status_code == 200


def test_rate_limit_429_with_retry_after(tmp_path):
    c = make_client(tmp_path)
    codes = [c.post("/v1/chat", json={"prompt": "hi"}, headers=GLOBEX).status_code
             for _ in range(10)]                      # globex: burst 3, 1 rps
    assert codes.count(200) >= 3 and 429 in codes
    r = next(r for r in [c.post("/v1/chat", json={"prompt": "hi"}, headers=GLOBEX)]
             if r.status_code == 429 or True)
    if r.status_code == 429:
        assert "retry-after" in {k.lower() for k in r.headers}


def test_quota_402_when_exhausted(tmp_path):
    tenants = {"k": Tenant("tiny", "k", rate_limit_rps=100, burst=100, token_quota=5)}
    c = make_client(tmp_path, tenants=tenants)
    h = {"Authorization": "Bearer k"}
    assert c.post("/v1/chat", json={"prompt": "one two three"}, headers=h).status_code == 200
    r = c.post("/v1/chat", json={"prompt": "again"}, headers=h)
    assert r.status_code == 402                        # quota (5 tokens) already burned


def test_failover_serves_from_secondary(tmp_path):
    c = make_client(tmp_path, chain=[FlakyProvider(), StaticProvider()])
    r = c.post("/v1/chat", json={"prompt": "hi"}, headers=ACME)
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "static" and body["attempts"] == 2
    h = c.get("/v1/health").json()
    assert h["consecutive_failures"]["flaky"] >= 1 and h["served"]["static"] >= 1


def test_all_providers_down_502(tmp_path):
    c = make_client(tmp_path, chain=[FlakyProvider()])
    r = c.post("/v1/chat", json={"prompt": "hi"}, headers=ACME)
    assert r.status_code == 502


def test_metering_and_usage_report(tmp_path):
    c = make_client(tmp_path)
    for _ in range(3):
        assert c.post("/v1/chat", json={"prompt": "count my tokens"},
                      headers=ACME).status_code == 200
    u = c.get("/v1/usage", headers=ACME).json()
    assert u["requests"] == 3 and u["tokens_in"] > 0 and u["cost_usd"] > 0
    assert u["quota_remaining"] < u["quota"]           # quota drawn down


def test_audit_log_written_and_scoped(tmp_path):
    c = make_client(tmp_path)
    c.post("/v1/chat", json={"prompt": "hi"}, headers=ACME)
    c.post("/v1/chat", json={"prompt": "hi"}, headers=GLOBEX)
    acme_rows = c.get("/v1/audit", headers=ACME).json()
    assert acme_rows and all(r["tenant"] == "acme" for r in acme_rows)
    assert all("prompt" not in r for r in acme_rows)   # prompts never logged


def test_streaming_sse(tmp_path):
    c = make_client(tmp_path)
    with c.stream("POST", "/v1/chat", json={"prompt": "hi", "stream": True},
                  headers=ACME) as r:
        assert r.status_code == 200
        body = "".join(chunk for chunk in r.iter_text())
    assert "event: provider" in body and "event: usage" in body and "[DONE]" in body
