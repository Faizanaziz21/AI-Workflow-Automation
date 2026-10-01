"""AI gateway: the only path from workflows to LLMs.

Responsibilities
* Model routing — primary model plus ordered fallbacks (possibly on other providers).
* Reliability — per-model retries with exponential backoff for transient errors (429/5xx/timeouts).
* Structured output — JSON-schema enforcement: provider-native structured output where supported, then local
  validation; malformed output triggers a *repair* turn that feeds the validation errors back to the model.
* Accounting — every call (success or failure) is persisted to ``ai_calls`` with tokens, cost, latency,
  prompt key/version and whether a fallback served it; Prometheus counters are updated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import jsonschema
from sqlalchemy import select

from app.ai.pricing import cost_usd
from app.ai.provider_connectors import AIProviderConnector
from app.ai.providers.base import strip_code_fences
from app.ai.types import ChatMessage, ChatRequest, ChatResponse, EmbeddingResponse, ProviderError
from app.connectors.sdk import ConnectorError
from app.core.metrics import AI_CALLS, AI_COST, AI_LATENCY, AI_TOKENS
from app.db.models import AICall, Connection, Organization

logger = logging.getLogger(__name__)


class AIError(Exception):
    def __init__(self, message: str, *, retryable: bool, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.attempts = attempts


@dataclass
class ModelRef:
    connection_id: str | None = None
    model: str | None = None


@dataclass
class AIRequest:
    prompt: str
    system: str | None = None
    history: list[ChatMessage] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    models: list[ModelRef] = field(default_factory=lambda: [ModelRef()])
    temperature: float | None = None
    max_tokens: int = 2048
    effort: str | None = None
    max_retries_per_model: int = 2
    max_repair_attempts: int = 2
    timeout: float = 120.0
    prompt_key: str | None = None
    prompt_version: int | None = None
    operation: str = "chat"


@dataclass
class AIResult:
    text: str
    data: Any
    provider: str
    model: str
    connection_id: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    attempts: int
    fallback_used: bool
    calls: list[dict[str, Any]]

    def usage(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "fallback_used": self.fallback_used,
        }


def validate_output(text: str, schema: dict[str, Any]) -> tuple[Any, list[str]]:
    try:
        data = json.loads(strip_code_fences(text))
    except json.JSONDecodeError as exc:
        return None, [f"Response is not valid JSON: {exc.msg} at position {exc.pos}"]
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    messages = [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors[:10]]
    return data, messages


ConnectionResolver = Callable[[str], Awaitable[Any]]


class AIGateway:
    def __init__(
        self,
        session_factory: Any,
        *,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        resolve_connection: ConnectionResolver,
        execution_id: uuid.UUID | None = None,
        node_id: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session_factory = session_factory
        self.org_id = org_id
        self.workspace_id = workspace_id
        self.resolve_connection = resolve_connection
        self.execution_id = execution_id
        self.node_id = node_id
        self.sleep = sleep
        self._pricing: dict[str, Any] | None = None

    async def _pricing_overrides(self) -> dict[str, Any]:
        if self._pricing is None:
            async with self.session_factory() as s:
                org = await s.get(Organization, self.org_id)
                self._pricing = dict((org.settings or {}).get("ai_pricing", {})) if org else {}
        return self._pricing

    async def default_connection_id(self) -> str:
        async with self.session_factory() as s:
            org = await s.get(Organization, self.org_id)
            preferred = (org.settings or {}).get("ai_default_connection_ids", {}) if org else {}
            if isinstance(preferred, dict) and preferred.get(str(self.workspace_id)):
                return str(preferred[str(self.workspace_id)])
            rows = (
                (
                    await s.execute(
                        select(Connection)
                        .where(
                            Connection.org_id == self.org_id,
                            Connection.workspace_id == self.workspace_id,
                            Connection.connector_key.in_(["anthropic", "openai", "gemini", "openai_compatible"]),
                        )
                        .order_by(Connection.created_at)
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            raise AIError("No AI provider connection configured in this workspace", retryable=False, attempts=[])
        return str(rows[0].id)

    async def _record(self, **fields: Any) -> None:
        try:
            async with self.session_factory() as s:
                s.add(AICall(org_id=self.org_id, execution_id=self.execution_id, node_id=self.node_id, **fields))
                await s.commit()
        except Exception:  # accounting must never break the workflow
            logger.exception("failed to record AI call")

    async def complete(self, req: AIRequest) -> AIResult:
        calls: list[dict[str, Any]] = []
        last_error: AIError | None = None
        overrides = await self._pricing_overrides()
        models = req.models or [ModelRef()]
        for idx, ref in enumerate(models):
            is_fallback = idx > 0
            try:
                connection_id = ref.connection_id or await self.default_connection_id()
                resolved = await self.resolve_connection(connection_id)
            except AIError as exc:
                last_error = exc
                continue
            except ConnectorError as exc:
                last_error = AIError(exc.message, retryable=False, attempts=calls)
                continue
            connector = resolved.connector
            if not isinstance(connector, AIProviderConnector):
                last_error = AIError(
                    f"Connection {connection_id} is not an AI provider", retryable=False, attempts=calls
                )
                continue
            provider = connector.provider
            model = ref.model or getattr(resolved.context.config, "default_model", None)
            if not model:
                last_error = AIError("No model specified", retryable=False, attempts=calls)
                continue
            messages = [*req.history, ChatMessage("user", req.prompt)]
            retries = 0
            repairs = 0
            while True:
                chat_req = ChatRequest(
                    model=model,
                    messages=list(messages),
                    system=req.system,
                    json_schema=req.output_schema,
                    temperature=req.temperature,
                    max_tokens=req.max_tokens,
                    effort=req.effort,
                    timeout=req.timeout,
                )
                started = time.perf_counter()
                attempt_no = len(calls) + 1
                try:
                    resp: ChatResponse = await provider.chat(resolved.context, chat_req)
                except ProviderError as exc:
                    latency = int((time.perf_counter() - started) * 1000)
                    calls.append(
                        {
                            "provider": provider.name,
                            "model": model,
                            "status": "error",
                            "error": exc.message,
                            "latency_ms": latency,
                        }
                    )
                    AI_CALLS.labels(provider.name, model, "error").inc()
                    await self._record(
                        provider=provider.name,
                        model=model,
                        operation=req.operation,
                        prompt_key=req.prompt_key,
                        prompt_version=req.prompt_version,
                        status="error",
                        error=exc.message[:2000],
                        latency_ms=latency,
                        attempt=attempt_no,
                        is_fallback=is_fallback,
                    )
                    last_error = AIError(exc.message, retryable=exc.retryable, attempts=calls)
                    if exc.retryable and retries < req.max_retries_per_model:
                        retries += 1
                        await self.sleep(min(30.0, 0.5 * (2**retries)) * (0.5 + random.random()))  # noqa: S311
                        continue
                    break  # next model
                latency = int((time.perf_counter() - started) * 1000)
                cost = cost_usd(provider.name, model, resp.input_tokens, resp.output_tokens, overrides)
                AI_LATENCY.labels(provider.name).observe(latency / 1000)
                AI_TOKENS.labels(provider.name, model, "input").inc(resp.input_tokens)
                AI_TOKENS.labels(provider.name, model, "output").inc(resp.output_tokens)
                AI_COST.labels(provider.name, model).inc(float(cost))
                data: Any = None
                errors: list[str] = []
                if req.output_schema is not None:
                    data, errors = validate_output(resp.text, req.output_schema)
                status = "success" if not errors else "invalid_output"
                AI_CALLS.labels(provider.name, model, status).inc()
                calls.append(
                    {
                        "provider": provider.name,
                        "model": resp.model,
                        "status": status,
                        "input_tokens": resp.input_tokens,
                        "output_tokens": resp.output_tokens,
                        "cost_usd": float(cost),
                        "latency_ms": latency,
                        "errors": errors or None,
                        "served_by_provider_fallback": resp.served_by_fallback,
                    }
                )
                await self._record(
                    provider=provider.name,
                    model=resp.model or model,
                    operation=req.operation,
                    prompt_key=req.prompt_key,
                    prompt_version=req.prompt_version,
                    status=status,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    cost_usd=cost,
                    latency_ms=latency,
                    attempt=attempt_no,
                    is_fallback=is_fallback or resp.served_by_fallback,
                    error="; ".join(errors)[:2000] if errors else None,
                )
                if errors:
                    last_error = AIError(
                        "Model output failed schema validation: " + "; ".join(errors[:3]),
                        retryable=False,
                        attempts=calls,
                    )
                    if repairs < req.max_repair_attempts:
                        repairs += 1
                        messages.append(ChatMessage("assistant", resp.text[:20000]))
                        messages.append(
                            ChatMessage(
                                "user",
                                (
                                    "Your previous response did not satisfy the required JSON schema. Problems:\n- "
                                    + "\n- ".join(errors)
                                    + "\nReturn ONLY the corrected JSON value."
                                ),
                            )
                        )
                        continue
                    break
                total_in = sum(c.get("input_tokens", 0) for c in calls)
                total_out = sum(c.get("output_tokens", 0) for c in calls)
                total_cost = sum(Decimal(str(c.get("cost_usd", 0))) for c in calls)
                return AIResult(
                    text=resp.text,
                    data=data if req.output_schema is not None else None,
                    provider=provider.name,
                    model=resp.model or model,
                    connection_id=connection_id,
                    input_tokens=total_in,
                    output_tokens=total_out,
                    cost_usd=float(total_cost),
                    latency_ms=sum(c.get("latency_ms", 0) for c in calls),
                    attempts=len(calls),
                    fallback_used=is_fallback or resp.served_by_fallback,
                    calls=calls,
                )
        if last_error is None:
            last_error = AIError("No model available", retryable=False, attempts=calls)
        last_error.attempts = calls
        raise last_error

    async def embed(self, ref: ModelRef, texts: list[str]) -> tuple[EmbeddingResponse, str, float]:
        connection_id = ref.connection_id or await self.default_connection_id()
        resolved = await self.resolve_connection(connection_id)
        connector = resolved.connector
        if not isinstance(connector, AIProviderConnector):
            raise AIError("Connection is not an AI provider", retryable=False, attempts=[])
        model = ref.model or getattr(resolved.context.config, "default_embedding_model", None)
        if not model:
            raise AIError("No embedding model configured", retryable=False, attempts=[])
        started = time.perf_counter()
        try:
            resp = await connector.provider.embed(resolved.context, texts, model)
        except ProviderError as exc:
            await self._record(
                provider=connector.provider.name,
                model=model,
                operation="embedding",
                status="error",
                error=exc.message[:2000],
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            raise AIError(exc.message, retryable=exc.retryable, attempts=[]) from exc
        cost = cost_usd(connector.provider.name, model, resp.input_tokens, 0, await self._pricing_overrides())
        await self._record(
            provider=connector.provider.name,
            model=model,
            operation="embedding",
            status="success",
            input_tokens=resp.input_tokens,
            cost_usd=cost,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        AI_CALLS.labels(connector.provider.name, model, "success").inc()
        AI_TOKENS.labels(connector.provider.name, model, "input").inc(resp.input_tokens)
        return resp, connector.provider.name, float(cost)
