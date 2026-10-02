from __future__ import annotations

from tests.conftest import add_user
from tests.factories import linear

DEF_V1 = linear(("set", "logic.set_variable", {"assignments": {"greeting": "hello"}}))
DEF_V2 = linear(
    ("set", "logic.set_variable", {"assignments": {"greeting": "hi"}}),
    ("delay", "logic.delay", {"amount": 1, "unit": "seconds"}),
)


async def create(client, tenant, name="Lead flow", definition=DEF_V1):
    r = await client.post(
        tenant.ws("/workflows"), headers=tenant.headers, json={"name": name, "definition": definition}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_lifecycle_publish_versions_diff_rollback(client, tenant):
    wf = await create(client, tenant)
    assert wf["draft"]["version"] == 1 and wf["published"] is None
    base = tenant.ws(f"/workflows/{wf['id']}")
    r = await client.post(f"{base}/publish", headers=tenant.headers, json={"change_note": "first"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    # Editing after publish creates draft v2 without touching v1.
    r = await client.put(f"{base}/draft", headers=tenant.headers, json={"definition": DEF_V2})
    assert r.status_code == 200 and r.json()["version"] == 2
    v1 = (await client.get(f"{base}/versions/1", headers=tenant.headers)).json()
    assert v1["definition"]["nodes"][1]["config"]["assignments"]["greeting"] == "hello"
    r = await client.post(f"{base}/publish", headers=tenant.headers, json={})
    assert r.json()["version"] == 2
    versions = (await client.get(f"{base}/versions", headers=tenant.headers)).json()
    assert [(v["version"], v["status"]) for v in versions] == [(2, "published"), (1, "archived")]
    diff = (await client.get(f"{base}/versions/diff", headers=tenant.headers, params={"from": 1, "to": 2})).json()
    assert [n["id"] for n in diff["nodes_added"]] == ["delay"]
    assert diff["nodes_changed"][0]["changes"][0]["after"] == "hi"
    r = await client.post(f"{base}/rollback", headers=tenant.headers, json={"version": 1})
    assert r.status_code == 200 and r.json()["version"] == 3
    detail = (await client.get(base, headers=tenant.headers)).json()
    assert detail["published"]["version"] == 3
    assert detail["published"]["definition_hash"] == v1["definition_hash"]
    actions = {
        i["action"]
        for i in (
            await client.get(
                "/api/v1/audit-logs", headers=tenant.headers, params={"resource_id": wf["id"], "limit": 100}
            )
        ).json()["items"]
    }
    assert {"workflow.created", "workflow.modified", "workflow.published", "workflow.rolled_back"} <= actions


async def test_publish_blocked_by_validation_errors(client, tenant):
    bad = linear(("x", "logic.delay", {"amount": -1}))
    wf = await create(client, tenant, definition=bad)
    r = await client.post(tenant.ws(f"/workflows/{wf['id']}/publish"), headers=tenant.headers, json={})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["errors"]


async def test_optimistic_concurrency(client, tenant):
    wf = await create(client, tenant)
    base = tenant.ws(f"/workflows/{wf['id']}")
    rev = wf["draft"]["revision"]
    r1 = await client.put(
        f"{base}/draft", headers=tenant.headers, json={"definition": DEF_V2, "expected_revision": rev}
    )
    assert r1.status_code == 200
    r2 = await client.put(
        f"{base}/draft", headers=tenant.headers, json={"definition": DEF_V1, "expected_revision": rev}
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "revision_conflict"


async def test_clone_and_archive(client, tenant):
    wf = await create(client, tenant)
    base = tenant.ws(f"/workflows/{wf['id']}")
    r = await client.post(f"{base}/clone", headers=tenant.headers, json={"name": "Copy"})
    assert r.status_code == 201 and r.json()["name"] == "Copy"
    r = await client.post(f"{base}/archive", headers=tenant.headers)
    assert r.json()["status"] == "archived"
    r = await client.put(f"{base}/draft", headers=tenant.headers, json={"definition": DEF_V2})
    assert r.status_code == 409
    listing = (await client.get(tenant.ws("/workflows"), headers=tenant.headers)).json()
    assert wf["id"] not in [w["id"] for w in listing["items"]]


async def test_webhook_trigger_activation(client, tenant):
    d = linear(("set", "logic.set_variable", {"assignments": {"a": 1}}), trigger="trigger.webhook")
    wf = await create(client, tenant, definition=d)
    base = tenant.ws(f"/workflows/{wf['id']}")
    info = (await client.get(f"{base}/trigger", headers=tenant.headers)).json()
    assert info["active"] is False
    await client.post(f"{base}/publish", headers=tenant.headers, json={})
    info = (await client.get(f"{base}/trigger", headers=tenant.headers)).json()
    assert info["webhook_url"].startswith("http") and info["signing_secret"] == "whsec_••••••••"
    revealed = (await client.get(f"{base}/trigger", headers=tenant.headers, params={"reveal_secret": True})).json()
    assert revealed["signing_secret"].startswith("whsec_") and "•" not in revealed["signing_secret"]
    # Token is stable across republish.
    await client.put(f"{base}/draft", headers=tenant.headers, json={"definition": d | {"variables": {"v": 2}}})
    await client.post(f"{base}/publish", headers=tenant.headers, json={})
    info2 = (await client.get(f"{base}/trigger", headers=tenant.headers)).json()
    assert info2["webhook_url"] == info["webhook_url"]


async def test_rbac_on_workflows(client, tenant, make_tenant):
    wf = await create(client, tenant)
    base = tenant.ws(f"/workflows/{wf['id']}")
    viewer = await add_user(client, tenant, "viewer")
    operator = await add_user(client, tenant, "operator")
    assert (await client.get(base, headers=viewer["headers"])).status_code == 200
    assert (
        await client.put(f"{base}/draft", headers=viewer["headers"], json={"definition": DEF_V2})
    ).status_code == 403
    assert (await client.post(f"{base}/publish", headers=operator["headers"], json={})).status_code == 403
    other = await make_tenant()
    assert (await client.get(base, headers=other.headers)).status_code == 404
    assert (await client.get(other.ws(f"/workflows/{wf['id']}"), headers=other.headers)).status_code == 404


async def test_publish_rejects_foreign_connection(client, make_tenant):
    a = await make_tenant()
    b = await make_tenant()
    conn = (
        await client.post(
            b.ws("/connections"),
            headers=b.headers,
            json={"name": "B slack", "connector_key": "slack", "credentials": {"bot_token": "xoxb-b"}},
        )
    ).json()
    d = linear(("post", "slack.post_message", {"connection_id": conn["id"], "channel": "#x", "text": "hi"}))
    wf = await create(client, a, definition=d)
    r = await client.post(a.ws(f"/workflows/{wf['id']}/publish"), headers=a.headers, json={})
    assert r.status_code == 422
    assert "does not exist" in r.json()["error"]["message"]


async def test_node_catalog(client, tenant):
    r = await client.get("/api/v1/node-types", headers=tenant.headers)
    types = {n["type"]: n for n in r.json()}
    for required in (
        "trigger.webhook",
        "logic.if",
        "logic.loop",
        "data.http_request",
        "ai.decision",
        "human.approval",
        "crm.upsert_contact",
        "comm.email",
        "slack.post_message",
        "jira.create_issue",
    ):
        assert required in types
    assert types["logic.if"]["handles"] == ["true", "false"]
    assert types["ai.classify"]["dynamic_handles"] is True
    assert "connection_id" in types["hubspot.create_deal"]["config_schema"]["properties"]
