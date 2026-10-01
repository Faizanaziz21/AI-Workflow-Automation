"""AI layer: gateway (schema enforcement, repair, retries, fallback, cost), providers, AI nodes, prompts, usage."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import httpx
import httpx2
import pytest
import respx
from sqlalchemy import select

from app.ai.gateway import AIError, AIGateway, AIRequest, ModelRef
from app.ai.pricing import cost_usd
from app.ai.providers import anthropic as anthropic_provider
from app.ai.types import ChatMessage, ChatRequest, ProviderError
from app.db.models import AICall
from app.db.session import get_sessionmaker
from app.services import connections as connection_service
from tests.conftest import drain, get_execution, node_runs, publish, run
from tests.factories import definition, edge, linear, node

LLM = "http://llm.test/v1/chat/completions"


def completion(content: str, prompt_tokens: int = 100, completion_tokens: int = 20) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-1",
            "model": "llama3.1:8b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


async def make_conn(client, tenant, key="openai_compatible", name=None, config=None, creds=None) -> str:
    cfg = config or {"base_url": "http://llm.test/v1", "default_model": "llama3.1:8b"}
    r = await client.post(
        tenant.ws("/connections"),
        headers=tenant.headers,
        json={
            "name": name or f"llm-{uuid.uuid4().hex[:6]}",
            "connector_key": key,
            "config": cfg,
            "credentials": creds if creds is not None else {},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def gateway(tenant) -> AIGateway:
    sm = get_sessionmaker()

    async def resolve(cid: str):
        async with sm() as s:
            return await connection_service.resolve(
                s, uuid.UUID(tenant.org_id), cid, workspace_id=uuid.UUID(tenant.workspace_id)
            )

    async def no_sleep(_):
        return None

    return AIGateway(
        sm,
        org_id=uuid.UUID(tenant.org_id),
        workspace_id=uuid.UUID(tenant.workspace_id),
        resolve_connection=resolve,
        sleep=no_sleep,
    )


SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string", "enum": ["sales", "support"]}, "confidence": {"type": "number"}},
    "required": ["category", "confidence"],
    "additionalProperties": False,
}


@respx.mock
async def test_schema_repair_loop(client, tenant):
    conn = await make_conn(client, tenant)
    route = respx.post(LLM)
    route.side_effect = [
        completion("Sure! The category is sales."),
        completion('```json\n{"category": "billing", "confidence": 0.9}\n```'),
        completion('{"category": "sales", "confidence": 0.93}'),
    ]
    result = await gateway(tenant).complete(
        AIRequest(prompt="Classify: I want a demo", output_schema=SCHEMA, models=[ModelRef(conn)], prompt_key="t")
    )
    assert result.data == {"category": "sales", "confidence": 0.93}
    assert result.attempts == 3 and result.cost_usd == 0.0
    last = json.loads(route.calls[-1].request.content)
    assert "did not satisfy the required JSON schema" in last["messages"][-1]["content"]
    assert last["response_format"] == {"type": "json_object"}
    async with get_sessionmaker()() as s:
        statuses = [
            c.status
            for c in (
                await s.execute(
                    select(AICall).where(AICall.org_id == uuid.UUID(tenant.org_id)).order_by(AICall.created_at)
                )
            )
            .scalars()
            .all()
        ]
    assert statuses[-3:] == ["invalid_output", "invalid_output", "success"]


@respx.mock
async def test_repairs_exhausted_raises(client, tenant):
    conn = await make_conn(client, tenant)
    respx.post(LLM).mock(return_value=completion("nope"))
    with pytest.raises(AIError) as ei:
        await gateway(tenant).complete(
            AIRequest(prompt="x", output_schema=SCHEMA, models=[ModelRef(conn)], max_repair_attempts=1)
        )
    assert "schema validation" in ei.value.message and len(ei.value.attempts) == 2


@respx.mock
async def test_retries_then_fallback_model_with_costs(client, tenant):
    primary = await make_conn(
        client,
        tenant,
        "openai",
        config={"base_url": "https://openai.test/v1", "default_model": "gpt-4.1-mini"},
        creds={"api_key": "sk-test-key-123456789012345"},
    )
    local = await make_conn(client, tenant)
    failing = respx.post("https://openai.test/v1/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": {"message": "overloaded"}})
    )
    respx.post(LLM).mock(return_value=completion('{"category": "support", "confidence": 0.7}'))
    result = await gateway(tenant).complete(
        AIRequest(prompt="Help!", output_schema=SCHEMA, models=[ModelRef(primary), ModelRef(local)])
    )
    assert failing.call_count == 3  # 1 + 2 retries
    assert result.fallback_used and result.provider == "openai_compatible"
    assert result.data["category"] == "support"
    sent = json.loads(failing.calls[0].request.content)
    assert sent["response_format"]["type"] == "json_schema" and sent["response_format"]["json_schema"]["strict"]
    assert failing.calls[0].request.headers["authorization"] == "Bearer sk-test-key-123456789012345"


@respx.mock
async def test_non_retryable_error_skips_retries(client, tenant):
    primary = await make_conn(
        client, tenant, "openai", config={"base_url": "https://openai.test/v1"}, creds={"api_key": "sk-bad"}
    )
    bad = respx.post("https://openai.test/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": "invalid key"}})
    )
    with pytest.raises(AIError) as ei:
        await gateway(tenant).complete(AIRequest(prompt="x", models=[ModelRef(primary)]))
    assert bad.call_count == 1 and not ei.value.retryable


def test_cost_calculation():
    assert cost_usd("openai", "gpt-4.1-mini", 1000, 500) == Decimal("0.0012")
    assert cost_usd("anthropic", "claude-opus-5-5", 1_000_000, 100_000) == Decimal("6")
    assert cost_usd("openai", "gpt-4o-2024-08-06", 1_000_000, 0) == Decimal("2.5")  # dated snapshot prefix
    assert cost_usd("openai_compatible", "llama3", 10_000, 10_000) == 0
    assert cost_usd("openai", "my-ft-model", 10, 10, {"my-ft-model": {"input": 1, "output": 1}}) == Decimal("0.00002")


async def test_anthropic_provider_structured_output_and_fallback_flag():
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": '{"category": "sales", "confidence": 0.97}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 42, "output_tokens": 9},
            },
        )

    anthropic_provider.http_client_factory = lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    anthropic_provider._clients.clear()
    try:
        from app.ai.provider_connectors import AnthropicConfig, ApiKeyCredentials
        from app.connectors.sdk import ConnectorContext

        ctx = ConnectorContext(
            org_id="o",
            connection_id="c",
            config=AnthropicConfig(),
            credentials=ApiKeyCredentials(api_key="sk-ant-test"),
            http=httpx.AsyncClient(),
        )
        resp = await anthropic_provider.AnthropicProvider().chat(
            ctx,
            ChatRequest(
                model="claude-opus-5-5",
                messages=[ChatMessage("user", "Classify")],
                system="You classify.",
                json_schema=SCHEMA,
                effort="low",
                temperature=0.2,
            ),
        )
        assert resp.text.startswith('{"category"') and resp.input_tokens == 42 and resp.output_tokens == 9
        body = captured["body"]
        assert body["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"}
        assert body["fallbacks"] == "default" and "temperature" not in body
        assert "server-side-fallback-2026-07-01" in captured["headers"].get("anthropic-beta", "")
        assert body["system"] == "You classify."
    finally:
        anthropic_provider.http_client_factory = None
        anthropic_provider._clients.clear()


async def test_anthropic_refusal_is_non_retryable():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "msg_2",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [],
                "stop_reason": "refusal",
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 0},
            },
        )

    anthropic_provider.http_client_factory = lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    anthropic_provider._clients.clear()
    try:
        from app.ai.provider_connectors import AnthropicConfig, ApiKeyCredentials
        from app.connectors.sdk import ConnectorContext

        ctx = ConnectorContext(
            org_id="o",
            connection_id="c",
            config=AnthropicConfig(),
            credentials=ApiKeyCredentials(api_key="sk-ant-test"),
            http=httpx.AsyncClient(),
        )
        with pytest.raises(ProviderError) as ei:
            await anthropic_provider.AnthropicProvider().chat(
                ctx, ChatRequest(model="claude-haiku-4-5", messages=[ChatMessage("user", "x")])
            )
        assert ei.value.refusal and not ei.value.retryable
    finally:
        anthropic_provider.http_client_factory = None
        anthropic_provider._clients.clear()


@respx.mock
async def test_gemini_provider_request_shape():
    from app.ai.provider_connectors import GeminiConfig
    from app.ai.provider_connectors import GeminiConnector as _G
    from app.connectors.sdk import ConnectorContext

    route = respx.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '{"category":"sales","confidence":1}'}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
            },
        )
    )
    ctx = ConnectorContext(
        org_id="o",
        connection_id="c",
        config=GeminiConfig(),
        credentials=_G.auth.credentials_model(api_key="AIza-test"),
        http=httpx.AsyncClient(),
    )
    resp = await _G.provider.chat(
        ctx,
        ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage("user", "hi")], json_schema=SCHEMA, system="sys"),
    )
    body = json.loads(route.calls[0].request.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert route.calls[0].request.headers["x-goog-api-key"] == "AIza-test"
    assert resp.input_tokens == 7 and resp.output_tokens == 3


@respx.mock
async def test_ai_nodes_in_workflow(client, tenant):
    conn = await make_conn(client, tenant)

    def responder(request):
        body = json.loads(request.content)
        text = body["messages"][-1]["content"]
        if "Classify this lead" in text:
            return completion(
                json.dumps(
                    {
                        "classification": "enterprise",
                        "priority": "high",
                        "confidence": 0.94,
                        "estimated_deal_value": 120000,
                        "reasoning_summary": "Large company",
                    }
                )
            )
        if "Route this email" in text:
            return completion(
                json.dumps(
                    {
                        "category": "billing",
                        "confidence": 0.88,
                        "urgency": "high",
                        "language": "en",
                        "reasoning_summary": "Invoice question",
                    }
                )
            )
        return completion(
            json.dumps({"decision": "refund", "confidence": 0.55, "reasoning_summary": "Unsure", "amount": 8000})
        )

    respx.post(LLM).mock(side_effect=responder)
    model = {"connection_id": conn}
    d = definition(
        [
            node("t", "trigger.manual"),
            node("lead", "ai.lead_classifier", {"lead": "{{ trigger.lead }}", "model": model}),
            node(
                "router",
                "ai.email_router",
                {
                    "sender": "{{ trigger.email['from'] }}",
                    "subject": "{{ trigger.email.subject }}",
                    "body": "{{ trigger.email.body }}",
                    "model": model,
                },
            ),
            node("billing", "logic.set_variable", {"assignments": {"queue": "billing"}}),
            node(
                "decide",
                "ai.decision",
                {
                    "goal": "Decide whether to refund",
                    "context": "{{ trigger }}",
                    "options": ["refund", "deny"],
                    "route_by_decision": True,
                    "min_confidence": 0.8,
                    "output_schema": {"amount": {"type": "number"}},
                    "model": model,
                },
            ),
            node("review", "logic.set_variable", {"assignments": {"needs_human": True}}),
        ],
        [
            edge("t", "lead"),
            edge("t", "router"),
            edge("router", "billing", "billing"),
            edge("t", "decide"),
            edge("decide", "review", "needs_review"),
        ],
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(
        client,
        tenant,
        wf,
        {
            "lead": {"company": "Acme", "employees": 5000},
            "email": {"from": "ap@acme.com", "subject": "Invoice", "body": "Wrong amount"},
        },
    )
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "COMPLETED", ex
    runs = await node_runs(client, tenant, ex_id)
    assert runs["lead"]["output"]["classification"] == "enterprise"
    assert runs["lead"]["output"]["usage"]["provider"] == "openai_compatible"
    assert runs["router"]["branches"] == ["billing"] and runs["billing"]["status"] == "COMPLETED"
    assert runs["decide"]["branches"] == ["needs_review"] and runs["decide"]["output"]["amount"] == 8000
    usage = (await client.get("/api/v1/ai/usage", headers=tenant.headers, params={"group_by": "prompt"})).json()
    keys = {g["key"] for g in usage["groups"]}
    assert {"lead_classifier", "email_router", "decision"} <= keys
    by_wf = (await client.get("/api/v1/ai/usage", headers=tenant.headers, params={"group_by": "workflow"})).json()
    assert by_wf["totals"]["calls"] >= 3


@respx.mock
async def test_prompt_versioning_and_pinning(client, tenant):
    conn = await make_conn(client, tenant)
    route = respx.post(LLM).mock(
        return_value=completion(
            json.dumps(
                {
                    "sentiment": "negative",
                    "score": -0.6,
                    "confidence": 0.9,
                    "emotions": ["frustration"],
                    "reasoning_summary": "Complaint",
                }
            )
        )
    )
    r = await client.post(
        "/api/v1/ai/prompts/sentiment/versions",
        headers=tenant.headers,
        json={
            "description": "Support-tuned",
            "system_prompt": "You are a support QA analyst.",
            "user_prompt": "CUSTOM-V1 analyse: {{ input.text }}",
        },
    )
    assert r.status_code == 201 and r.json()["version"] == 1
    versions = (await client.get("/api/v1/ai/prompts/sentiment/versions", headers=tenant.headers)).json()
    assert [v["version"] for v in versions] == [1, 0]
    d = linear(
        ("latest", "ai.sentiment", {"text": "This is unacceptable", "model": {"connection_id": conn}}),
        ("pinned", "ai.sentiment", {"text": "Still waiting", "model": {"connection_id": conn, "prompt_version": 0}}),
    )
    wf = await publish(client, tenant, d)
    await run(client, tenant, wf)
    await drain()
    prompts = [json.loads(c.request.content)["messages"][-1]["content"] for c in route.calls]
    assert prompts[0].startswith("CUSTOM-V1 analyse: This is unacceptable")
    assert prompts[1].startswith("Analyse the sentiment")
    async with get_sessionmaker()() as s:
        rows = (
            await s.execute(
                select(AICall.prompt_key, AICall.prompt_version).where(AICall.org_id == uuid.UUID(tenant.org_id))
            )
        ).all()
    assert ("sentiment", 1) in rows and ("sentiment", 0) in rows


def test_builtin_prompts_are_valid_templates():
    from app.ai.prompts import BUILTIN_PROMPTS
    from app.engine.expressions import extract_expressions, parse

    for p in BUILTIN_PROMPTS.values():
        for expr in extract_expressions(p.user) + extract_expressions(p.system):
            parse(expr)
