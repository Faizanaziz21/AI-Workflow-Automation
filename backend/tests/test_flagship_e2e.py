"""End-to-end: the flagship Enterprise Customer Support Automation template running against the sandbox
services through the real connectors (HubSpot, Jira, Slack, SendGrid, REST, OpenAI-compatible LLM, Postgres)."""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from app.core.security import sign_payload
from app.db.session import get_sessionmaker
from tests.conftest import drain, get_execution, node_runs

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mock-services"))
from sandbox.app import STORE
from sandbox.app import app as sandbox_app

SANDBOX = "http://sandbox.test"


@pytest.fixture
def sandbox():
    asgi = httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox_app), base_url=SANDBOX)

    async def forward(request: httpx.Request) -> httpx.Response:
        resp = await asgi.request(
            request.method,
            request.url.raw_path.decode(),
            content=request.content,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
        )
        return httpx.Response(resp.status_code, content=resp.content, headers=resp.headers)

    with respx.mock(assert_all_called=False) as router:
        router.route(host="sandbox.test").mock(side_effect=forward)
        STORE.clear()
        yield router


async def _install_flagship(client, tenant) -> tuple[str, str, str]:
    from app.demo import provision_demo_connections
    from app.services.auth import load_user_principal

    async with get_sessionmaker()() as s:
        principal = await load_user_principal(s, uuid.UUID(tenant.user_id), uuid.UUID(tenant.org_id))
        roles = await provision_demo_connections(s, principal, uuid.UUID(tenant.workspace_id), SANDBOX)
        await s.commit()
    templates = (await client.get("/api/v1/templates", headers=tenant.headers)).json()
    assert templates[0]["slug"] == "customer-support-automation" and templates[0]["featured"]
    assert len(templates) == 10
    r = await client.post(
        "/api/v1/templates/customer-support-automation/install",
        headers=tenant.headers,
        json={
            "workspace_id": tenant.workspace_id,
            "connections": {k: v for k, v in roles.items() if k in templates[0]["connection_roles"]},
        },
    )
    assert r.status_code == 201, r.text
    wf = r.json()["workflow_id"]
    r = await client.post(tenant.ws(f"/workflows/{wf}/publish"), headers=tenant.headers, json={})
    assert r.status_code == 200, r.text
    info = (
        await client.get(tenant.ws(f"/workflows/{wf}/trigger"), headers=tenant.headers, params={"reveal_secret": True})
    ).json()
    return wf, "/api/v1/hooks/" + info["webhook_url"].rsplit("/", 1)[-1], info["signing_secret"]


async def _send(client, path: str, secret: str, email: dict) -> str:
    raw = json.dumps(email).encode()
    ts = int(time.time())
    r = await client.post(
        path,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-FlowForge-Signature": sign_payload(secret, ts, raw),
            "X-FlowForge-Timestamp": str(ts),
            "Message-Id": f"<{uuid.uuid4()}@mail>",
        },
    )
    assert r.status_code == 202, r.text
    return r.json()["execution_id"]


async def test_flagship_support_automation(client, tenant, sandbox):
    from app.services.templates import seed_templates

    async with get_sessionmaker()() as s:
        await seed_templates(s)
        await s.commit()
    _, path, secret = await _install_flagship(client, tenant)

    # 1) Routine billing question from a mid-market customer -> confident, low risk -> auto-sent.
    simple = await _send(
        client,
        path,
        secret,
        {
            "from": "bob@initech.com",
            "subject": "Question about my invoice",
            "text": "Hi, where can I download last month's invoice and update our billing contact? Thanks, Bob",
        },
    )
    # 2) Furious enterprise customer charged twice -> agent review + escalation to senior manager.
    angry = await _send(
        client,
        path,
        secret,
        {
            "from": "jane@globex.com",
            "subject": "Charged twice AGAIN",
            "text": "This is unacceptable! I was charged twice again this month. Terrible service, I'm furious and "
            "disappointed. Fix it now! Regards, Jane",
        },
    )
    await drain(60)

    ex = await get_execution(client, tenant, simple)
    assert ex["status"] == "COMPLETED", ex
    runs = await node_runs(client, tenant, simple)
    assert runs["send_auto"]["status"] == "COMPLETED" and runs["agent_review"]["status"] == "SKIPPED"
    assert runs["p1_ticket"]["status"] == "SKIPPED"
    assert runs["priority"]["output"]["tier"] == "mid_market"
    assert runs["kb"]["output"]["body"][0]["id"] == "KB-103"
    emails = [e for e in STORE["emails"] if e["to"] == ["bob@initech.com"]]
    assert {e["subject"] for e in emails} >= {"Re: Question about my invoice"}
    assert any("How did we do?" in e["subject"] for e in emails)  # satisfaction survey
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(
                text("SELECT category, tier, auto_sent, root_cause FROM support_analytics WHERE execution_id = :e"),
                {"e": simple},
            )
        ).one()
    assert row.category == "billing" and row.auto_sent is True and row.tier == "mid_market"

    ex = await get_execution(client, tenant, angry)
    assert ex["status"] == "WAITING_APPROVAL"
    runs = await node_runs(client, tenant, angry)
    assert runs["sentiment"]["output"]["sentiment"] == "very_negative"
    assert runs["priority"]["output"] == {**runs["priority"]["output"], "tier": "enterprise", "priority": "P1"}
    assert runs["p1_ticket"]["status"] == "COMPLETED" and runs["p1_ticket"]["output"]["ticket"]["key"].startswith(
        "SUP-"
    )
    assert any("P1 escalation" in m["text"] for m in STORE["slack"])
    assert runs["send_auto"]["status"] == "SKIPPED"

    # Support agent edits the AI draft and approves it.
    inbox = (await client.get("/api/v1/approvals", headers=tenant.headers)).json()["items"]
    review = next(i for i in inbox if i["execution_id"] == angry)
    assert review["kind"] == "manual_review" and "jane@globex.com" in review["title"]
    edited = {
        "subject": "We're sorry — duplicate charge refunded",
        "body": "Hi Jane, the duplicate charge is refunded.",
    }
    r = await client.post(
        f"/api/v1/approvals/{review['id']}/approve",
        headers=tenant.headers,
        json={"comment": "Softened tone", "response_data": edited},
    )
    assert r.status_code == 200, r.text
    await drain(60)
    ex = await get_execution(client, tenant, angry)
    assert ex["status"] == "COMPLETED", ex
    sent = [e for e in STORE["emails"] if e["to"] == ["jane@globex.com"]]
    assert sent[0]["subject"] == edited["subject"]
    assert any(n.get("properties", {}).get("hs_note_body", "").startswith("Support ticket (P1") for n in STORE["notes"])
    timeline = (await client.get(f"/api/v1/executions/{angry}/events", headers=tenant.headers)).json()
    assert [e["type"] for e in timeline].count("approval_decided") == 1
    usage = (await client.get("/api/v1/ai/usage", headers=tenant.headers, params={"group_by": "workflow"})).json()
    assert usage["totals"]["calls"] >= 10 and usage["totals"]["input_tokens"] > 0
