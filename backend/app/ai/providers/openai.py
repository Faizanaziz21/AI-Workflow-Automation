"""OpenAI Chat Completions adapter — also serves OpenAI-compatible servers (Ollama, vLLM, LM Studio, Azure-style gateways)."""

from __future__ import annotations

from typing import Any

import httpx

from app.ai.providers.base import schema_instruction
from app.ai.types import ChatRequest, ChatResponse, EmbeddingResponse, ProviderError, classify_status
from app.connectors.sdk import ConnectorContext


def strict_compatible(schema: dict[str, Any]) -> bool:
    """OpenAI strict mode needs every object to list all properties as required and forbid extras."""
    if not isinstance(schema, dict):
        return True
    if schema.get("type") == "object" or "properties" in schema:
        props = schema.get("properties", {})
        if schema.get("additionalProperties", True) is not False:
            return False
        if set(schema.get("required", [])) != set(props):
            return False
        return all(strict_compatible(v) for v in props.values())
    if schema.get("type") == "array" and "items" in schema:
        return strict_compatible(schema["items"])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            return all(strict_compatible(s) for s in schema[key])
    return True


class OpenAIProvider:
    name = "openai"

    def __init__(
        self, default_base: str = "https://api.openai.com/v1", json_mode: str = "json_schema", name: str = "openai"
    ) -> None:
        self.name = name
        self.default_base = default_base
        self.json_mode = json_mode

    def _base(self, ctx: ConnectorContext) -> str:
        return str(getattr(ctx.config, "base_url", None) or self.default_base).rstrip("/")

    def _headers(self, ctx: ConnectorContext) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = getattr(ctx.credentials, "api_key", None)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        org = getattr(ctx.config, "organization", None)
        if org:
            headers["OpenAI-Organization"] = org
        return headers

    async def _post(self, ctx: ConnectorContext, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            resp = await ctx.http.post(
                f"{self._base(ctx)}{path}", json=body, headers=self._headers(ctx), timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name} request timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"{self.name} transport error: {exc}", retryable=True) from exc
        if not resp.is_success:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text[:300])
            except ValueError:
                detail = resp.text[:300]
            raise ProviderError(
                f"{self.name} HTTP {resp.status_code}: {detail}",
                retryable=classify_status(resp.status_code),
                status_code=resp.status_code,
            )
        data: dict[str, Any] = resp.json()
        return data

    async def chat(self, ctx: ConnectorContext, request: ChatRequest) -> ChatResponse:
        messages: list[dict[str, str]] = []
        system = request.system or ""
        json_mode = getattr(ctx.config, "json_mode", None) or self.json_mode
        body: dict[str, Any] = {"model": request.model, "max_completion_tokens": request.max_tokens}
        if request.json_schema is not None:
            if json_mode == "json_schema" and strict_compatible(request.json_schema):
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": request.schema_name, "schema": request.json_schema, "strict": True},
                }
            else:
                if json_mode != "none":
                    body["response_format"] = {"type": "json_object"}
                system = (system + "\n\n" if system else "") + schema_instruction(request.json_schema)
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)
        body["messages"] = messages
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.effort and getattr(ctx.config, "supports_reasoning_effort", False):
            body["reasoning_effort"] = request.effort
        data = await self._post(ctx, "/chat/completions", body, request.timeout)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise ProviderError(f"Model refused: {message['refusal']}", retryable=False, refusal=True)
        usage = data.get("usage") or {}
        return ChatResponse(
            text=message.get("content") or "",
            model=data.get("model", request.model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            finish_reason=choice.get("finish_reason"),
            provider_request_id=data.get("id"),
        )

    async def embed(self, ctx: ConnectorContext, texts: list[str], model: str) -> EmbeddingResponse:
        data = await self._post(ctx, "/embeddings", {"model": model, "input": texts}, 60)
        vectors = [item["embedding"] for item in sorted(data.get("data", []), key=lambda d: d.get("index", 0))]
        return EmbeddingResponse(
            vectors=vectors,
            model=data.get("model", model),
            input_tokens=int((data.get("usage") or {}).get("prompt_tokens", 0)),
        )

    async def list_models(self, ctx: ConnectorContext) -> list[str]:
        try:
            resp = await ctx.http.get(f"{self._base(ctx)}/models", headers=self._headers(ctx), timeout=15)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: {exc}", retryable=True) from exc
        if not resp.is_success:
            raise ProviderError(f"{self.name} HTTP {resp.status_code}", retryable=False, status_code=resp.status_code)
        return sorted(m["id"] for m in resp.json().get("data", []))
