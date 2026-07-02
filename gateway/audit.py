"""Append-only JSONL audit log — one record per request (id, tenant, provider, latency, status,
token counts). The compliance/debug trail; prompts are NOT logged by default (privacy)."""
from __future__ import annotations

import json
import threading
import time
import uuid


class Audit:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, **fields) -> str:
        rec = {"request_id": str(uuid.uuid4())[:8], "ts": round(time.time(), 3)} | fields
        line = json.dumps(rec, separators=(",", ":"))
        with self._lock:
            with open(self.path, "a") as f:
                f.write(line + "\n")
        return rec["request_id"]

    def tail(self, n: int = 20) -> list[dict]:
        try:
            with open(self.path) as f:
                lines = f.readlines()[-n:]
            return [json.loads(l) for l in lines]
        except FileNotFoundError:
            return []
