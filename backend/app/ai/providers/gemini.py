"""Google Gemini adapter (Generative Language REST API)."""

from __future__ import annotations

from typing import Any

import httpx

from app.ai.types import ChatRequest, ChatResponse, EmbeddingResponse, ProviderError, classify_status
from app.connectors.sdk import ConnectorContext


class GeminiProvider:
    name = "gemini"

    def _base(self, ctx: ConnectorContext) -> str:
        return str(getattr(ctx.config, "base_url", None) or "https://generativelanguage.googleapis.com").rstrip("/")

    async def _post(self, ctx: ConnectorContext, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            resp = await ctx.http.post(
                f"{self._base(ctx)}/v1beta/{path}",
                json=body,
                timeout=timeout,
                headers={"x-goog-api-key": ctx.credentials.api_key},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("Gemini request timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"Gemini transport error: {exc}", retryable=True) from exc
        if not resp.is_success:
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except ValueError:
                detail = resp.text[:300]
            raise ProviderError(
                f"Gemini HTTP {resp.status_code}: {detail}",
                retryable=classify_status(resp.status_code),
                status_code=resp.status_code,
            )
        data: dict[str, Any] = resp.json()
        return data

    async def chat(self, ctx: ConnectorContext, request: ChatRequest) -> ChatResponse:
        contents = [
            {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
            for m in request.messages
            if m.role != "system"
        ]
        generation: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.json_schema is not None:
            generation["responseMimeType"] = "application/json"
            generation["responseJsonSchema"] = request.json_schema
        body: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        data = await self._post(ctx, f"models/{request.model}:generateContent", body, request.timeout)
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise ProviderError(f"Gemini returned no output ({reason})", retryable=False, refusal=True)
        cand = candidates[0]
        if cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise ProviderError(f"Gemini blocked the response ({cand['finishReason']})", retryable=False, refusal=True)
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        usage = data.get("usageMetadata") or {}
        return ChatResponse(
            text=text,
            model=data.get("modelVersion", request.model),
            input_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0)),
            finish_reason=cand.get("finishReason"),
            provider_request_id=data.get("responseId"),
        )

    async def embed(self, ctx: ConnectorContext, texts: list[str], model: str) -> EmbeddingResponse:
        body = {"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]}} for t in texts]}
        data = await self._post(ctx, f"models/{model}:batchEmbedContents", body, 60)
        vectors = [e["values"] for e in data.get("embeddings", [])]
        approx_tokens = sum(len(t) // 4 for t in texts)
        return EmbeddingResponse(vectors=vectors, model=model, input_tokens=approx_tokens)

    async def list_models(self, ctx: ConnectorContext) -> list[str]:
        try:
            resp = await ctx.http.get(
                f"{self._base(ctx)}/v1beta/models", headers={"x-goog-api-key": ctx.credentials.api_key}, timeout=15
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini: {exc}", retryable=True) from exc
        if not resp.is_success:
            raise ProviderError(f"Gemini HTTP {resp.status_code}", retryable=False, status_code=resp.status_code)
        return sorted(m["name"].removeprefix("models/") for m in resp.json().get("models", []))
