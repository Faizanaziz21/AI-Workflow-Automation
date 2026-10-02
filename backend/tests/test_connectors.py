from __future__ import annotations

import json
import os
import time

import httpx
import pytest
import respx

from app.connectors.builtin.email.client import build_message, parse_message
from app.connectors.registry import connectors_with_capability, get_connector
from app.connectors.sdk import ConnectorContext, ConnectorError
from app.connectors.sqlutil import looks_like_write, to_positional, to_pyformat, validate_identifier
from app.core.egress import EgressPolicyTransport


def ctx_for(key: str, config: dict, creds: dict, **kw) -> ConnectorContext:
    c = get_connector(key)
    return ConnectorContext(
        org_id="org",
        connection_id="conn",
        config=c.auth.config_model.model_validate(config),
        credentials=c.auth.credentials_model.model_validate(creds),
        http=httpx.AsyncClient(),
        **kw,
    )


def test_capability_index():
    assert set(connectors_with_capability("crm.upsert_contact")) == {"hubspot", "pipedrive"}
    assert set(connectors_with_capability("sms.send")) == {"twilio", "vonage"}
    assert set(connectors_with_capability("email.send")) == {"email", "sendgrid"}


def test_connector_describe_has_schemas():
    d = get_connector("hubspot").describe()
    action = next(a for a in d["actions"] if a["key"] == "upsert_contact")
    assert action["input_schema"]["properties"]["email"]
    assert d["auth"]["credentials_schema"]["required"] == ["access_token"]


@respx.mock
async def test_slack_post_message():
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "123.45"})
    )
    respx.get("https://slack.com/api/chat.getPermalink").mock(
        return_value=httpx.Response(200, json={"ok": True, "permalink": "https://x.slack.com/p1"})
    )
    ctx = ctx_for("slack", {"default_channel": "#sales"}, {"bot_token": "xoxb-test"})
    out = await get_connector("slack").run_action("post_message", ctx, {"text": "New lead!"})
    assert out == {"channel": "C1", "ts": "123.45", "permalink": "https://x.slack.com/p1"}
    sent = json.loads(respx.calls[0].request.content)
    assert sent["channel"] == "#sales"
    assert respx.calls[0].request.headers["authorization"] == "Bearer xoxb-test"


@respx.mock
async def test_slack_api_errors_classified():
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
    )
    ctx = ctx_for("slack", {}, {"bot_token": "xoxb-test"})
    with pytest.raises(ConnectorError) as ei:
        await get_connector("slack").run_action("post_message", ctx, {"channel": "#x", "text": "hi"})
    assert not ei.value.retryable
    respx.post("https://slack.com/api/chat.postMessage").mock(return_value=httpx.Response(503))
    with pytest.raises(ConnectorError) as ei:
        await get_connector("slack").run_action("post_message", ctx, {"channel": "#x", "text": "hi"})
    assert ei.value.retryable


@respx.mock
async def test_hubspot_upsert_creates_then_updates():
    base = "https://api.hubapi.com"
    search = respx.post(f"{base}/crm/v3/objects/contacts/search")
    search.side_effect = [
        httpx.Response(200, json={"results": []}),
        httpx.Response(200, json={"results": [{"id": "77", "properties": {"email": "a@b.co"}}]}),
    ]
    respx.post(f"{base}/crm/v3/objects/contacts").mock(
        return_value=httpx.Response(201, json={"id": "77", "properties": {"email": "a@b.co", "firstname": "Ann"}})
    )
    respx.patch(f"{base}/crm/v3/objects/contacts/77").mock(
        return_value=httpx.Response(200, json={"id": "77", "properties": {"email": "a@b.co", "company": "Acme"}})
    )
    ctx = ctx_for("hubspot", {"portal_id": "42"}, {"access_token": "pat-1"}, idempotency_key="exec:node:1")
    hub = get_connector("hubspot")
    first = await hub.run_action("upsert_contact", ctx, {"email": "a@b.co", "first_name": "Ann"})
    assert first["created"] is True
    assert first["contact"]["url"] == "https://app.hubspot.com/contacts/42/contact/77"
    second = await hub.run_action("upsert_contact", ctx, {"email": "a@b.co", "company": "Acme"})
    assert second["created"] is False
    create_call = next(
        c for c in respx.calls if c.request.method == "POST" and c.request.url.path.endswith("/contacts")
    )
    assert create_call.request.headers["idempotency-key"] == "exec:node:1"


@respx.mock
async def test_jira_create_issue_uses_adf():
    respx.post("https://acme.atlassian.net/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "1001", "key": "SUP-12"})
    )
    ctx = ctx_for("jira", {"site_url": "https://acme.atlassian.net"}, {"email": "a@acme.com", "api_token": "t"})
    out = await get_connector("jira").run_action(
        "create_issue",
        ctx,
        {"project": "SUP", "summary": "Refund request", "description": "Line one\nLine two", "priority": "High"},
    )
    assert out["ticket"]["key"] == "SUP-12"
    assert out["ticket"]["url"] == "https://acme.atlassian.net/browse/SUP-12"
    body = json.loads(respx.calls[0].request.content)
    assert body["fields"]["description"]["type"] == "doc"
    assert body["fields"]["priority"] == {"name": "High"}


@respx.mock
async def test_rest_oauth_client_credentials_caches_token():
    token = respx.post("https://auth.example.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    api = respx.get("https://erp.example.com/api/orders").mock(return_value=httpx.Response(200, json={"orders": []}))
    saved = []

    async def save(c):
        saved.append(c)

    ctx = ctx_for(
        "http_rest",
        {
            "base_url": "https://erp.example.com/api",
            "auth_mode": "oauth2_client_credentials",
            "token_url": "https://auth.example.com/token",
        },
        {"client_id": "id", "client_secret": "sec"},
        save_credentials=save,
    )
    rest = get_connector("http_rest")
    out = await rest.run_action("request", ctx, {"method": "GET", "path": "/orders"})
    await rest.run_action("request", ctx, {"method": "GET", "path": "/orders"})
    assert out["status"] == 200 and out["body"] == {"orders": []}
    assert token.call_count == 1
    assert api.calls[0].request.headers["authorization"] == "Bearer tok-1"
    assert saved and saved[0]["access_token"] == "tok-1"


@respx.mock
async def test_google_drive_refreshes_expired_token():
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
    )
    respx.get("https://www.googleapis.com/drive/v3/files").mock(
        return_value=httpx.Response(200, json={"files": [{"id": "f1", "name": "a.pdf", "mimeType": "application/pdf"}]})
    )
    saved = []

    async def save(c):
        saved.append(c)

    ctx = ctx_for(
        "google_drive",
        {"client_id": "cid"},
        {"client_secret": "s", "refresh_token": "r", "access_token": "old", "expires_at": time.time() - 10},
        save_credentials=save,
    )
    out = await get_connector("google_drive").run_action("list_files", ctx, {"folder_id": "abc"})
    assert out["files"][0]["id"] == "f1"
    assert respx.calls[-1].request.headers["authorization"] == "Bearer fresh"
    assert saved[0]["refresh_token"] == "r"


@respx.mock
async def test_twilio_send_sms_and_validation():
    sid = "AC" + "0" * 32
    respx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json").mock(
        return_value=httpx.Response(201, json={"sid": "SM1", "status": "queued"})
    )
    ctx = ctx_for(
        "twilio", {"account_sid": sid, "from_number": "+15550001111"}, {"auth_token": "t"}, idempotency_key="k1"
    )
    out = await get_connector("twilio").run_action("send_sms", ctx, {"to": "+15550002222", "body": "Hi"})
    assert out == {"message_id": "SM1", "status": "queued", "provider": "twilio"}
    with pytest.raises(Exception):
        await get_connector("twilio").run_action("send_sms", ctx, {"to": "not-a-number", "body": "Hi"})


@respx.mock
async def test_teams_adaptive_card():
    respx.post("https://acme.webhook.office.com/hook").mock(return_value=httpx.Response(202))
    ctx = ctx_for("teams", {}, {"webhook_url": "https://acme.webhook.office.com/hook"})
    out = await get_connector("teams").run_action(
        "post_message", ctx, {"title": "Escalation", "text": "Enterprise customer unhappy", "facts": {"Priority": "P1"}}
    )
    assert out["delivered"]
    payload = json.loads(respx.calls[0].request.content)
    card = payload["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["body"][2]["facts"] == [{"title": "Priority", "value": "P1"}]


def test_email_message_has_deterministic_message_id():
    msg = build_message(
        from_address="ops@acme.com",
        from_name="Ops",
        to=["a@b.com"],
        cc=[],
        subject="Hello",
        text="body",
        html="<p>body</p>",
        reply_to=None,
        headers={},
        idempotency_key="exec-1-node",
    )
    assert msg["Message-ID"] == "<exec-1-node@acme.com>"
    parsed = parse_message(7, bytes(msg))
    assert parsed["subject"] == "Hello" and parsed["from_address"] == "ops@acme.com"
    assert "body" in parsed["text"] and parsed["html"]


def test_sql_param_conversion_and_safety():
    sql, args = to_positional(
        "SELECT * FROM t WHERE a = :a AND b::text = ':nope' AND c = :a OR d = :d", {"a": 1, "d": 2}
    )
    assert sql == "SELECT * FROM t WHERE a = $1 AND b::text = ':nope' AND c = $1 OR d = $2"
    assert args == [1, 2]
    assert (
        to_pyformat("SELECT * FROM t WHERE n LIKE '50%' AND id = :id", {"id": 3})
        == "SELECT * FROM t WHERE n LIKE '50%%' AND id = %(id)s"
    )
    with pytest.raises(ValueError):
        to_positional("SELECT :missing", {})
    assert looks_like_write("DELETE FROM users")
    assert looks_like_write("SELECT 1; DROP TABLE users")
    assert not looks_like_write("SELECT * FROM users -- delete")
    with pytest.raises(ValueError):
        validate_identifier("users; DROP TABLE x")
    assert validate_identifier("public.orders") == "public.orders"


async def test_postgres_connector_against_real_database():
    url = os.environ["FF_DATABASE_URL"]
    ctx = ctx_for(
        "postgres",
        {"host": "localhost", "database": url.rsplit("/", 1)[1], "ssl_mode": "disable"},
        {"username": "flowforge", "password": "flowforge"},
    )
    pg = get_connector("postgres")
    await pg.run_action(
        "query", ctx, {"sql": "CREATE TABLE IF NOT EXISTS conn_test (id serial primary key, email text)"}
    )
    await pg.run_action("query", ctx, {"sql": "INSERT INTO conn_test (email) VALUES (:e)", "params": {"e": "x@y.z"}})
    out = await pg.run_action(
        "query", ctx, {"sql": "SELECT email FROM conn_test WHERE email = :e", "params": {"e": "x@y.z"}}
    )
    assert out["rows"][0]["email"] == "x@y.z"
    # Injection attempt stays a bound value.
    out = await pg.run_action(
        "query", ctx, {"sql": "SELECT count(*) AS n FROM conn_test WHERE email = :e", "params": {"e": "' OR '1'='1"}}
    )
    assert out["rows"][0]["n"] == 0
    # Trigger polling: first poll seeds cursor, then returns only new rows.
    poll = await pg.poll("new_rows", ctx, {"table": "conn_test", "cursor_column": "id"}, {})
    assert poll.items == []
    await pg.run_action("query", ctx, {"sql": "INSERT INTO conn_test (email) VALUES ('new@y.z')"})
    poll2 = await pg.poll("new_rows", ctx, {"table": "conn_test", "cursor_column": "id"}, poll.state)
    assert [r["email"] for r in poll2.items] == ["new@y.z"]
    ro = ctx_for(
        "postgres",
        {"host": "localhost", "database": url.rsplit("/", 1)[1], "ssl_mode": "disable", "read_only": True},
        {"username": "flowforge", "password": "flowforge"},
    )
    with pytest.raises(ConnectorError):
        await pg.run_action("query", ro, {"sql": "DELETE FROM conn_test"})


async def test_egress_policy_blocks_private_addresses(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "allow_private_network_egress", False)
    async with httpx.AsyncClient(transport=EgressPolicyTransport()) as client:
        for url in (
            "http://127.0.0.1:8000/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/",
            "http://[::1]/",
            "file:///etc/passwd",
        ):
            with pytest.raises(httpx.RequestError):
                await client.get(url)
    monkeypatch.setattr(get_settings(), "egress_allowlist", ["127.0.0.1"])
    async with httpx.AsyncClient(transport=EgressPolicyTransport()) as client:
        with pytest.raises(httpx.ConnectError):  # allowed by policy, nothing listening on the port
            await client.get("http://127.0.0.1:1/")


@pytest.mark.parametrize(
    ("exc", "fragment"),
    [(httpx.ReadTimeout("slow"), "timed out"), (httpx.ConnectError("refused"), "transport error")],
)
@respx.mock
async def test_http_transport_failures_are_retryable(exc, fragment):
    from app.connectors.builtin.http_rest.client import perform_request
    from app.connectors.builtin.http_rest.schemas import RequestInput

    respx.get("https://api.example.com/x").mock(side_effect=exc)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConnectorError) as info:
            await perform_request(http, "https://api.example.com/x", RequestInput(path="/x"))
    assert info.value.retryable is True and fragment in info.value.message


def test_runner_classifies_raw_transport_errors_as_retryable():
    from app.engine.runner import _error_dict

    err = _error_dict(httpx.ReadTimeout(""), attempt=1, secret_values=[])
    assert err["retryable"] is True and err["code"] == "transport_error"
