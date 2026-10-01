"""Anthropic Claude adapter built on the official ``anthropic`` SDK.

* Structured output uses ``output_config.format`` (JSON schema) so the first text block is valid JSON.
* Reasoning depth is controlled with ``output_config.effort``; current Claude models think adaptively and
  reject sampling parameters, so ``temperature`` is not forwarded.
* For models with safety classifiers, server-side refusal fallbacks (``fallbacks="default"``) are enabled by
  default so a declined request is transparently re-run on Anthropic's recommended fallback model. A refusal
  that survives the fallback surfaces as a non-retryable ``ProviderError(refusal=True)`` and the gateway moves
  on to the next model in the workflow's own fallback chain.
"""

from __future__ import annotations

from typing import Any

import anthropic

from app.ai.types import ChatRequest, ChatResponse, EmbeddingResponse, ProviderError
from app.connectors.sdk import ConnectorContext

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept ``fallbacks="default"`` on the Claude API.
SERVER_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}

_clients: dict[tuple[str, str], anthropic.AsyncAnthropic] = {}


def _client(api_key: str, base_url: str | None) -> anthropic.AsyncAnthropic:
    key = (api_key, base_url or "")
    client = _clients.get(key)
    if client is None:
        # Retries/fallback are owned by the AI gateway so attempts are visible and costed.
        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url or None, max_retries=0)
        _clients[key] = client
    return client


def _supports_server_fallback(model: str) -> bool:
    return model in SERVER_FALLBACK_MODELS


class AnthropicProvider:
    name = "anthropic"

    async def chat(self, ctx: ConnectorContext, request: ChatRequest) -> ChatResponse:
        client = _client(ctx.credentials.api_key, getattr(ctx.config, "base_url", None))
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages if m.role != "system"],
            "timeout": request.timeout,
        }
        if request.system:
            params["system"] = request.system
        output_config: dict[str, Any] = {}
        if request.json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.json_schema}
        if request.effort in EFFORT_LEVELS:
            output_config["effort"] = request.effort
        if output_config:
            params["output_config"] = output_config
        use_fallback = getattr(ctx.config, "server_side_fallback", True) and _supports_server_fallback(request.model)
        try:
            if use_fallback:
                response = await client.beta.messages.create(
                    betas=[SERVER_FALLBACK_BETA], fallbacks="default", **params
                )
            else:
                response = await client.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"Anthropic rate limited: {exc.message}", retryable=True, status_code=429) from exc
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic rejected the API key", retryable=False, status_code=401) from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"Anthropic bad request: {exc.message}", retryable=False, status_code=400) from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(
                f"Anthropic model not found: {request.model}", retryable=False, status_code=404
            ) from exc
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code >= 500 or exc.status_code in (408, 409, 529)
            raise ProviderError(
                f"Anthropic HTTP {exc.status_code}: {exc.message}", retryable=retryable, status_code=exc.status_code
            ) from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderError("Anthropic request timed out", retryable=True) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Anthropic connection error: {exc}", retryable=True) from exc

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise ProviderError(f"Claude declined the request (category: {category})", retryable=False, refusal=True)
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        iterations = getattr(response.usage, "iterations", None) or []
        served_by_fallback = any(getattr(i, "type", None) == "fallback_message" for i in iterations)
        return ChatResponse(
            text=text,
            model=response.model,
            input_tokens=int(response.usage.input_tokens or 0),
            output_tokens=int(response.usage.output_tokens or 0),
            finish_reason=response.stop_reason,
            provider_request_id=getattr(response, "_request_id", None),
            served_by_fallback=served_by_fallback,
        )

    async def embed(self, ctx: ConnectorContext, texts: list[str], model: str) -> EmbeddingResponse:
        raise ProviderError(
            "Anthropic does not offer an embeddings endpoint; use an OpenAI, Gemini or local embedding connection",
            retryable=False,
        )

    async def list_models(self, ctx: ConnectorContext) -> list[str]:
        client = _client(ctx.credentials.api_key, getattr(ctx.config, "base_url", None))
        try:
            page = await client.models.list(limit=100)
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic rejected the API key", retryable=False, status_code=401) from exc
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic error: {exc}", retryable=True) from exc
        return sorted(m.id for m in page.data)
