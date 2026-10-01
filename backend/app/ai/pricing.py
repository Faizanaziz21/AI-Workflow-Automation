"""Model pricing (USD per 1M tokens) used for cost tracking.

Defaults are list prices at the time of writing; organisations override or extend them through
``organization.settings.ai_pricing = {"<model>": {"input": x, "output": y}}``. Local/self-hosted models
(``openai_compatible``) cost 0 unless configured. Unknown models are recorded with cost 0 and flagged.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # OpenAI
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    # Google
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-embedding-001": (0.15, 0.0),
}

FREE_PROVIDERS = {"openai_compatible"}
_MILLION = Decimal(1_000_000)


def price_for(provider: str, model: str, overrides: dict[str, Any] | None = None) -> tuple[Decimal, Decimal, bool]:
    """Return (input $/1M, output $/1M, known)."""
    overrides = overrides or {}
    if model in overrides:
        o = overrides[model]
        return Decimal(str(o.get("input", 0))), Decimal(str(o.get("output", 0))), True
    if provider in FREE_PROVIDERS:
        return Decimal(0), Decimal(0), True
    # Match exact id, else the longest known prefix (handles dated snapshots like gpt-4o-2024-08-06).
    if model in DEFAULT_PRICING:
        i, o = DEFAULT_PRICING[model]
        return Decimal(str(i)), Decimal(str(o)), True
    candidates = [k for k in DEFAULT_PRICING if model.startswith(k)]
    if candidates:
        i, o = DEFAULT_PRICING[max(candidates, key=len)]
        return Decimal(str(i)), Decimal(str(o)), True
    return Decimal(0), Decimal(0), False


def cost_usd(
    provider: str, model: str, input_tokens: int, output_tokens: int, overrides: dict[str, Any] | None = None
) -> Decimal:
    pin, pout, _ = price_for(provider, model, overrides)
    return ((pin * input_tokens) + (pout * output_tokens)) / _MILLION
