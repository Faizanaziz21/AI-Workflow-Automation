from __future__ import annotations

import httpx
import respx
from sqlalchemy import text

from app.db.session import get_sessionmaker
from tests.conftest import add_user

SLACK = {
    "name": "Sales Slack",
    "connector_key": "slack",
    "config": {"default_channel": "#sales"},
    "credentials": {"bot_token": "xoxb-super-secret-token-123"},
}


async def test_credentials_are_encrypted_and_never_returned(client, tenant):
    r = await client.post(tenant.ws("/connections"), headers=tenant.headers, json=SLACK)
    assert r.status_code == 201, r.text
    body = r.json()
    assert "xoxb-super-secret-token-123" not in r.text
    assert body["credential_fields"] == ["bot_token"]
    assert body["secret_version"] == 1
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(
                text("SELECT ciphertext FROM secrets s JOIN connections c ON c.secret_id = s.id WHERE c.id = :id"),
                {"id": body["id"]},
            )
        ).one()
        assert b"xoxb-super-secret-token-123" not in bytes(row[0])
    listing = await client.get(tenant.ws("/connections"), headers=tenant.headers)
    assert "xoxb-super-secret-token-123" not in listing.text
    audit = await client.get("/api/v1/audit-logs", headers=tenant.headers, params={"action": "connection.created"})
    assert audit.json()["items"] and "xoxb" not in audit.text


async def test_rotation_increments_version_and_keeps_masked_fields(client, tenant):
    conn = (
        await client.post(tenant.ws("/connections"), headers=tenant.headers, json={**SLACK, "name": "Rotating"})
    ).json()
    r = await client.post(
        tenant.ws(f"/connections/{conn['id']}/rotate"),
        headers=tenant.headers,
        json={"credentials": {"bot_token": "xoxb-new-token-456"}},
    )
    assert r.status_code == 200
    assert r.json()["secret_version"] == 2
    r = await client.patch(
        tenant.ws(f"/connections/{conn['id']}"),
        headers=tenant.headers,
        json={"config": {"default_channel": "#alerts"}, "credentials": {"bot_token": "••••••••"}},
    )
    assert r.json()["config"]["default_channel"] == "#alerts"
    assert r.json()["secret_version"] == 2  # masked placeholder does not overwrite the secret
    actions = [
        i["action"]
        for i in (
            await client.get("/api/v1/audit-logs", headers=tenant.headers, params={"resource_id": conn["id"]})
        ).json()["items"]
    ]
    assert "connection.credentials_rotated" in actions


async def test_invalid_credentials_do_not_echo_values(client, tenant):
    r = await client.post(
        tenant.ws("/connections"),
        headers=tenant.headers,
        json={
            "name": "bad",
            "connector_key": "twilio",
            "config": {"account_sid": "nope", "from_number": "+1555"},
            "credentials": {"auth_token": "secret-value-xyz"},
        },
    )
    assert r.status_code == 422
    assert "secret-value-xyz" not in r.text


async def test_connection_isolation_and_permissions(client, make_tenant):
    a = await make_tenant()
    b = await make_tenant()
    conn = (await client.post(a.ws("/connections"), headers=a.headers, json=SLACK)).json()
    # Other tenant: path workspace belongs to A -> 404 on workspace.
    r = await client.get(a.ws(f"/connections/{conn['id']}"), headers=b.headers)
    assert r.status_code == 404
    # Other tenant using own workspace with A's connection id -> 404.
    r = await client.get(b.ws(f"/connections/{conn['id']}"), headers=b.headers)
    assert r.status_code == 404
    viewer = await add_user(client, a, "viewer")
    r = await client.post(a.ws("/connections"), headers=viewer["headers"], json={**SLACK, "name": "v"})
    assert r.status_code == 403
    r = await client.get(a.ws("/connections"), headers=viewer["headers"])
    assert r.status_code == 200 and r.json()[0]["can_use"] is False


async def test_access_policy_restricts_use(client, tenant):
    conn = (
        await client.post(
            tenant.ws("/connections"),
            headers=tenant.headers,
            json={**SLACK, "name": "Finance only", "access_policy": {"roles": ["approver"]}},
        )
    ).json()
    operator = await add_user(client, tenant, "operator")
    r = await client.post(tenant.ws(f"/connections/{conn['id']}/test"), headers=operator["headers"])
    assert r.status_code == 403


@respx.mock
async def test_connection_test_endpoint(client, tenant, respx_mock):
    respx_mock.post("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": True, "team": "Acme", "user": "flowforge"})
    )
    conn = (
        await client.post(tenant.ws("/connections"), headers=tenant.headers, json={**SLACK, "name": "Testable"})
    ).json()
    r = await client.post(tenant.ws(f"/connections/{conn['id']}/test"), headers=tenant.headers)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    got = (await client.get(tenant.ws(f"/connections/{conn['id']}"), headers=tenant.headers)).json()
    assert got["status"] == "connected"


async def test_connector_catalog(client, tenant):
    r = await client.get("/api/v1/connectors", headers=tenant.headers)
    keys = {c["key"] for c in r.json()}
    assert {"slack", "hubspot", "jira", "google_drive", "postgres", "openai", "anthropic", "gemini"} <= keys
    r = await client.get("/api/v1/connectors", headers=tenant.headers, params={"category": "crm"})
    assert {c["key"] for c in r.json()} == {"hubspot", "pipedrive"}
