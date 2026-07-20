# llm-gateway

A **production LLM API gateway**, the layer a platform team puts between every internal app and
the model providers: **API-key auth**, **per-tenant rate limiting and token quotas**, **ordered
provider failover** (AWS Bedrock → Anthropic API), **SSE streaming passthrough**, **per-tenant
cost metering**, and an **append-only audit log**. One request path, every production concern.

```bash
cp .env.example .env          # Bedrock / Anthropic creds
uvicorn gateway.api:app       # run the gateway
curl -X POST localhost:8000/v1/chat \
  -H "Authorization: Bearer gw-acme-demo-key" \
  -d '{"prompt": "hello"}' -H 'content-type: application/json'
```

## Request path

```
Authorization: Bearer <key>
   │ auth (401)                     gateway/api.py
   ▼
token-bucket rate limit (429 + Retry-After)     gateway/ratelimit.py
   │
token quota check (402 when budget exhausted)   gateway/ratelimit.py
   │
ordered failover chain: bedrock → anthropic     gateway/failover.py + providers.py
   │  (per-provider consecutive-failure + served-by tracking, /v1/health)
   ▼
response (or SSE stream: provider / chunks / usage / [DONE])
   │
meter tokens + USD per tenant (/v1/usage)       gateway/metering.py
append audit record — prompts never logged      gateway/audit.py
```

Endpoints: `POST /v1/chat` (stream or not) · `GET /v1/usage` (tenant's tokens/cost/quota) ·
`GET /v1/health` (chain state) · `GET /v1/audit` (tenant-scoped tail) · `GET /healthz`.

## Verified against real infrastructure

Demo run with a **deliberately dead primary** in front of real AWS Bedrock:

```
chain: [flaky (down), bedrock]
POST /v1/chat → 200 | served by: bedrock | attempts: 2 | latency 2135ms
                usage: {tokens_in: 17, tokens_out: 32, cost_usd: $0.000177}
stream:        event:provider → data chunks → event:usage → [DONE]
usage report:  {requests: 2, tokens_in: 32, tokens_out: 41, cost_usd: $0.000237,
                quota_used: 73 / 200,000}
/v1/health:    {consecutive_failures: {flaky: 2, bedrock: 0}, served: {bedrock: 2}}
```

The client never saw the outage: the gateway absorbed the failed attempt, served from the next
provider, metered the *real* token usage into the tenant's bill, and recorded the whole thing in
the audit log.

## Design decisions worth asking about

- **Rate limit ≠ quota.** The token bucket bounds *burst pressure* (requests/sec, answers 429 +
  `Retry-After`); the quota bounds *total spend* (tokens, answers 402). A tenant can be inside
  their rate limit and still out of budget, production billing needs both.
- **Streaming failover only before first byte.** If a provider dies mid-stream the client has
  already received partial output, replaying on another provider would silently duplicate or
  contradict it. The gateway fails over only when an upstream dies *before* emitting anything;
  mid-stream failures surface as an SSE `error` event. An honest limitation, stated rather than
  hidden.
- **Prompts are never audit-logged.** The audit trail carries request id / tenant / provider /
  tokens / cost / latency / status, enough for compliance and debugging without turning the log
  into a PII store.
- **Providers are one interface.** `complete` / `complete_stream` returning `(text, Usage)`, 
  adding OpenAI/Gemini/vLLM upstreams is one class each; the failover chain is config
  (`GATEWAY_PROVIDERS=bedrock,anthropic`).

## Install & test

```bash
pip install -e ".[dev]"
pytest -q          # 8 passed, offline — auth 401, rate-limit 429+Retry-After, quota 402,
                   # failover to secondary, 502 when all down, metering math, tenant-scoped
                   # audit (no prompts), SSE streaming, all against stub upstreams
```

CI never calls a real provider; the real Bedrock path is validated by the failover demo above.

## Stack

FastAPI, token-bucket limiter + quota tracker (stdlib), ordered failover over
`anthropic[bedrock]` / `anthropic` SDK providers, SSE streaming, JSONL audit, pytest.

## License

MIT
