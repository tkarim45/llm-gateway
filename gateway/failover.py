"""Ordered provider failover. Walk the chain; on ProviderError move to the next and record the
failure. Tracks consecutive failures per provider and reports which provider actually served each
request — the observable the failover demo measures."""
from __future__ import annotations

import threading
import time

from .providers import ProviderError


class Failover:
    def __init__(self, chain: list) -> None:
        self.chain = chain
        self._lock = threading.Lock()
        self.failures: dict[str, int] = {p.name: 0 for p in chain}
        self.served: dict[str, int] = {p.name: 0 for p in chain}

    def complete(self, prompt: str, max_tokens: int):
        """Returns (text, usage, provider_name, attempts). Raises if the whole chain fails."""
        errors = []
        for i, p in enumerate(self.chain):
            t0 = time.perf_counter()
            try:
                text, usage = p.complete(prompt, max_tokens)
                with self._lock:
                    self.failures[p.name] = 0
                    self.served[p.name] += 1
                return text, usage, p.name, i + 1, (time.perf_counter() - t0) * 1000
            except ProviderError as e:
                with self._lock:
                    self.failures[p.name] += 1
                errors.append(str(e))
        raise ProviderError("all providers failed: " + " | ".join(errors))

    def stream(self, prompt: str, max_tokens: int):
        """Yields ('provider', name) once, then ('chunk', text)..., then ('usage', Usage).
        Fails over only if a provider dies BEFORE emitting its first chunk (a mid-stream failure
        can't be replayed transparently — that's surfaced to the client)."""
        errors = []
        for p in self.chain:
            gen = p.complete_stream(prompt, max_tokens)
            try:
                first = next(gen)
            except ProviderError as e:
                with self._lock:
                    self.failures[p.name] += 1
                errors.append(str(e))
                continue
            except StopIteration:
                continue
            with self._lock:
                self.served[p.name] += 1
            yield ("provider", p.name)
            item = first
            while True:
                if hasattr(item, "tokens_in"):          # the trailing Usage sentinel
                    yield ("usage", item)
                    return
                yield ("chunk", item)
                try:
                    item = next(gen)
                except StopIteration:
                    return
        raise ProviderError("all providers failed: " + " | ".join(errors))

    def health(self) -> dict:
        with self._lock:
            return {"chain": [p.name for p in self.chain],
                    "consecutive_failures": dict(self.failures),
                    "served": dict(self.served)}
