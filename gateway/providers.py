"""Upstream LLM providers behind one interface, so failover is a chain walk.

  * BedrockProvider    — Claude on AWS Bedrock (primary in the default chain)
  * AnthropicProvider  — first-party Anthropic API (secondary)
  * StaticProvider     — deterministic echo upstream for tests/CI (never in the default chain)
  * FlakyProvider      — always-fails upstream used by the failover demo/tests

Each returns (text, tokens_in, tokens_out). complete_stream yields text chunks then a final
usage dict — the gateway passes chunks through as SSE.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .config import Config


@dataclass
class Usage:
    tokens_in: int
    tokens_out: int


class ProviderError(Exception):
    pass


class _AnthropicLike:
    _client = None
    _model = ""
    name = "base"

    def complete(self, prompt: str, max_tokens: int) -> tuple[str, Usage]:
        try:
            msg = self._client.messages.create(
                model=self._model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}])
        except Exception as e:
            raise ProviderError(f"{self.name}: {e}") from e
        text = "".join(b.text for b in msg.content if b.type == "text")
        return text, Usage(msg.usage.input_tokens, msg.usage.output_tokens)

    def complete_stream(self, prompt: str, max_tokens: int):
        try:
            with self._client.messages.stream(
                    model=self._model, max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}]) as stream:
                for chunk in stream.text_stream:
                    yield chunk
                final = stream.get_final_message()
            yield Usage(final.usage.input_tokens, final.usage.output_tokens)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"{self.name}: {e}") from e


class BedrockProvider(_AnthropicLike):
    name = "bedrock"

    def __init__(self, cfg: Config) -> None:
        from anthropic import AnthropicBedrock
        self._client = AnthropicBedrock(aws_region=cfg.aws_region,
                                        timeout=cfg.provider_timeout_s)
        self._model = cfg.bedrock_model


class AnthropicProvider(_AnthropicLike):
    name = "anthropic"

    def __init__(self, cfg: Config) -> None:
        import anthropic
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ProviderError("anthropic: no ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(timeout=cfg.provider_timeout_s)
        self._model = cfg.anthropic_model


class StaticProvider:
    """Deterministic echo — the test/CI upstream."""
    name = "static"

    def complete(self, prompt: str, max_tokens: int):
        text = f"echo:{prompt[:64]}"
        return text, Usage(len(prompt.split()), len(text.split()))

    def complete_stream(self, prompt: str, max_tokens: int):
        text = f"echo:{prompt[:64]}"
        for word in text.split(":"):
            yield word
        yield Usage(len(prompt.split()), len(text.split()))


class FlakyProvider:
    """Always fails — stands in for a down upstream in failover tests/demos."""
    name = "flaky"

    def complete(self, prompt: str, max_tokens: int):
        raise ProviderError("flaky: upstream unavailable")

    def complete_stream(self, prompt: str, max_tokens: int):
        raise ProviderError("flaky: upstream unavailable")
        yield  # pragma: no cover


def build_chain(cfg: Config) -> list:
    """Instantiate the configured provider chain, skipping providers that can't construct."""
    out = []
    for name in cfg.providers:
        try:
            if name == "bedrock":
                out.append(BedrockProvider(cfg))
            elif name == "anthropic":
                out.append(AnthropicProvider(cfg))
            elif name == "static":
                out.append(StaticProvider())
            elif name == "flaky":
                out.append(FlakyProvider())
        except Exception:
            continue
    if not out:
        out = [StaticProvider()]
    return out
