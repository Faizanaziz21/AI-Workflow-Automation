from __future__ import annotations

import asyncio
import json
import time

from sqlalchemy import text

from app.core.security import sign_payload
from app.db.session import get_sessionmaker
from tests.conftest import drain, get_execution, node_runs, publish
from tests.factories import linear


async def _webhook_wf(client, tenant, **cfg):
    d = linear(
        ("echo", "logic.set_variable", {"assignments": {"name": "{{ trigger.body.name }}"}}),
        trigger="trigger.webhook",
        trigger_config=cfg,
    )
    wf = await publish(client, tenant, d)
    info = (
        await client.get(tenant.ws(f"/workflows/{wf}/trigger"), headers=tenant.headers, params={"reveal_secret": True})
    ).json()
    return wf, info


def _signed(secret: str, body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    ts = int(time.time())
    return raw, {
        "X-FlowForge-Signature": sign_payload(secret, ts, raw),
        "X-FlowForge-Timestamp": str(ts),
        "Content-Type": "application/json",
    }


async def test_signed_webhook_and_dedupe(client, tenant):
    _, info = await _webhook_wf(client, tenant)
    path = (
        info["webhook_url"].split("testserver")[-1]
        if "testserver" in info["webhook_url"]
        else "/api/v1/hooks/" + info["webhook_url"].rsplit("/", 1)[-1]
    )
    raw, headers = _signed(info["signing_secret"], {"name": "Ada"})
    r = await client.post(path, content=raw, headers={**headers, "Idempotency-Key": "evt-1"})
    assert r.status_code == 202, r.text
    ex_id = r.json()["execution_id"]
    dup = await client.post(path, content=raw, headers={**headers, "Idempotency-Key": "evt-1"})
    assert dup.status_code == 200 and dup.json()["duplicate"] is True and dup.json()["execution_id"] == ex_id
    bad = await client.post(path, content=raw, headers={**headers, "X-FlowForge-Signature": "v1=deadbeef"})
    assert bad.status_code == 401
    tampered = await client.post(path, content=json.dumps({"name": "Eve"}).encode(), headers=headers)
    assert tampered.status_code == 401
    assert (await client.post("/api/v1/hooks/does-not-exist", content=raw, headers=headers)).status_code == 404
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "COMPLETED" and ex["trigger_type"] == "webhook"
    assert "x-flowforge-signature" not in ex["trigger_payload"]["headers"]
    assert (await node_runs(client, tenant, ex_id))["echo"]["output"] == {"name": "Ada"}


async def test_webhook_wait_for_response(client, tenant):
    from app.workers.worker import Worker

    d = linear(
        ("reply", "comm.webhook_response", {"status_code": 201, "body": {"greeting": "Hi {{ trigger.body.name }}"}}),
        trigger="trigger.webhook",
        trigger_config={"authentication": "none", "response_mode": "wait_for_response"},
    )
    wf = await publish(client, tenant, d)
    info = (await client.get(tenant.ws(f"/workflows/{wf}/trigger"), headers=tenant.headers)).json()
    path = "/api/v1/hooks/" + info["webhook_url"].rsplit("/", 1)[-1]
    worker = Worker(concurrency=4, worker_id="sync-worker")
    task = asyncio.create_task(worker.run())
    try:
        r = await client.post(path, json={"name": "Grace"})
    finally:
        worker.stop()
        await task
    assert r.status_code == 201 and r.json() == {"greeting": "Hi Grace"}
    assert r.headers["X-FlowForge-Execution-Id"]


async def test_api_event_trigger_with_filter(client, tenant):
    d = linear(
        ("a", "logic.set_variable", {"assignments": {"order": "{{ trigger.data.id }}"}}),
        trigger="trigger.api_event",
        trigger_config={"event_name": "order.created", "filter": "{{ data.total > 100 }}"},
    )
    await publish(client, tenant, d)
    r = await client.post(
        "/api/v1/events",
        headers=tenant.headers,
        json={"name": "order.created", "data": {"id": "o-1", "total": 500}, "event_id": "e-1"},
    )
    assert r.status_code == 202
    started = [t for t in r.json()["triggered"] if t["status"] == "started"]
    assert len(started) == 1
    r = await client.post(
        "/api/v1/events",
        headers=tenant.headers,
        json={"name": "order.created", "data": {"id": "o-2", "total": 5}, "event_id": "e-2"},
    )
    assert r.json()["triggered"][0]["status"] == "filtered"
    r = await client.post(
        "/api/v1/events",
        headers=tenant.headers,
        json={"name": "order.created", "data": {"id": "o-1", "total": 500}, "event_id": "e-1"},
    )
    assert r.json()["triggered"][0]["status"] == "duplicate"
    await drain()
    ex_id = started[0]["execution_id"]
    assert (await node_runs(client, tenant, ex_id))["a"]["output"] == {"order": "o-1"}


async def test_file_upload_trigger_and_csv_processing(client, tenant):
    d = linear(
        ("parse", "data.csv", {"file_id": "{{ trigger.file.file_id }}"}),
        ("count", "logic.set_variable", {"assignments": {"rows": "{{ nodes.parse.output.count }}"}}),
        trigger="trigger.file_uploaded",
        trigger_config={"source": "platform", "filename_pattern": "*.csv"},
    )
    await publish(client, tenant, d)
    csv_bytes = b"email,score\na@x.com,90\nb@x.com,40\n"
    r = await client.post(
        tenant.ws("/files"), headers=tenant.headers, files={"file": ("leads.csv", csv_bytes, "text/csv")}
    )
    assert r.status_code == 201, r.text
    assert len(r.json()["triggered_executions"]) == 1
    r2 = await client.post(
        tenant.ws("/files"), headers=tenant.headers, files={"file": ("notes.txt", b"x", "text/plain")}
    )
    assert r2.json()["triggered_executions"] == []
    await drain()
    ex_id = r.json()["triggered_executions"][0]
    runs = await node_runs(client, tenant, ex_id)
    assert runs["parse"]["output"]["rows"][0] == {"email": "a@x.com", "score": "90"}
    assert runs["count"]["output"] == {"rows": 2}


async def test_cron_schedule_fires_exactly_once(client, tenant):
    from app.workers.scheduler import Scheduler

    d = linear(
        ("a", "logic.set_variable", {"assignments": {"at": "{{ trigger.scheduled_for }}"}}),
        trigger="trigger.schedule",
        trigger_config={"cron": "*/5 * * * *", "timezone": "Europe/Berlin"},
    )
    wf = await publish(client, tenant, d)
    info = (await client.get(tenant.ws(f"/workflows/{wf}/trigger"), headers=tenant.headers)).json()
    assert info["next_run_at"]
    async with get_sessionmaker()() as s:
        await s.execute(
            text("UPDATE workflow_triggers SET next_run_at = now() - interval '1 minute' WHERE workflow_id = :w"),
            {"w": wf},
        )
        await s.commit()
    await asyncio.gather(Scheduler().tick(), Scheduler().tick())
    await Scheduler().tick()
    listing = (await client.get("/api/v1/executions", headers=tenant.headers, params={"workflow_id": wf})).json()
    assert listing["total"] == 1
    info2 = (await client.get(tenant.ws(f"/workflows/{wf}/trigger"), headers=tenant.headers)).json()
    assert info2["next_run_at"] > info["next_run_at"] or info2["last_run_at"]
    await drain()
    assert listing["items"][0]["trigger_type"] == "schedule"


async def test_db_change_polling_trigger(client, tenant):
    from app.workers.scheduler import Scheduler

    async with get_sessionmaker()() as s:
        await s.execute(text("CREATE TABLE IF NOT EXISTS cdc_orders (id serial primary key, customer text)"))
        await s.commit()
    conn = (
        await client.post(
            tenant.ws("/connections"),
            headers=tenant.headers,
            json={
                "name": "Orders DB",
                "connector_key": "postgres",
                "config": {"host": "localhost", "database": "flowforge_test", "ssl_mode": "disable"},
                "credentials": {"username": "flowforge", "password": "flowforge"},
            },
        )
    ).json()
    d = linear(
        ("seen", "logic.set_variable", {"assignments": {"customer": "{{ trigger.row.customer }}"}}),
        trigger="trigger.db_change",
        trigger_config={
            "connection_id": conn["id"],
            "table": "cdc_orders",
            "cursor_column": "id",
            "poll_interval_seconds": 15,
        },
    )
    wf = await publish(client, tenant, d)
    await Scheduler().tick()  # first poll seeds the cursor
    await drain()
    async with get_sessionmaker()() as s:
        await s.execute(text("INSERT INTO cdc_orders (customer) VALUES ('Initech'), ('Globex')"))
        await s.execute(text("UPDATE workflow_triggers SET next_run_at = now() WHERE workflow_id = :w"), {"w": wf})
        await s.commit()
    await Scheduler().tick()
    await drain()
    listing = (await client.get("/api/v1/executions", headers=tenant.headers, params={"workflow_id": wf})).json()
    customers = set()
    for item in listing["items"]:
        customers.add((await node_runs(client, tenant, item["id"]))["seen"]["output"]["customer"])
    assert customers == {"Initech", "Globex"}


async def test_app_event_trigger_polls_any_connector_trigger(client, tenant):
    """The generic App trigger runs a connector-declared polling trigger (HubSpot new_contacts here)."""
    import httpx
    import respx

    from app.workers.scheduler import Scheduler

    contact = {
        "id": "901",
        "properties": {"email": "nia@globex.com", "firstname": "Nia", "createdate": "2030-01-01T00:00:00Z"},
    }
    conn = (
        await client.post(
            tenant.ws("/connections"),
            headers=tenant.headers,
            json={
                "name": "CRM",
                "connector_key": "hubspot",
                "config": {"api_base_url": "https://crm.test", "portal_id": "1"},
                "credentials": {"access_token": "pat-test"},
            },
        )
    ).json()
    d = linear(
        ("seen", "logic.set_variable", {"assignments": {"email": "{{ trigger.email }}"}}),
        trigger="trigger.app_event",
        trigger_config={
            "connection_id": conn["id"],
            "event": "new_contacts",
            "settings": {"batch_size": 10},
            "poll_interval_seconds": 15,
        },
    )
    wf = await publish(client, tenant, d)
    with respx.mock(assert_all_called=True) as router:
        search = router.post("https://crm.test/crm/v3/objects/contacts/search").mock(
            side_effect=[httpx.Response(200, json={"results": []}), httpx.Response(200, json={"results": [contact]})]
        )
        await Scheduler().tick()  # first poll establishes the cursor
        await drain()
        async with get_sessionmaker()() as s:
            await s.execute(text("UPDATE workflow_triggers SET next_run_at = now() WHERE workflow_id = :w"), {"w": wf})
            await s.commit()
        await Scheduler().tick()
        await drain()
    assert search.call_count == 2
    assert json.loads(search.calls[1].request.content)["limit"] == 10
    listing = (await client.get("/api/v1/executions", headers=tenant.headers, params={"workflow_id": wf})).json()
    assert [i["trigger_type"] for i in listing["items"]] == ["app_event"]
    assert (await node_runs(client, tenant, listing["items"][0]["id"]))["seen"]["output"] == {"email": "nia@globex.com"}


async def test_app_event_trigger_reports_unknown_event(client, tenant):
    from app.workers.scheduler import Scheduler

    conn = (
        await client.post(
            tenant.ws("/connections"),
            headers=tenant.headers,
            json={"name": "API", "connector_key": "http_rest", "config": {"base_url": "https://api.test"}},
        )
    ).json()
    d = linear(
        ("seen", "logic.set_variable", {"assignments": {"x": 1}}),
        trigger="trigger.app_event",
        trigger_config={"connection_id": conn["id"], "event": "new_contacts"},
    )
    wf = await publish(client, tenant, d)
    await Scheduler().tick()
    await drain()
    info = (await client.get(tenant.ws(f"/workflows/{wf}/trigger"), headers=tenant.headers)).json()
    assert "has no 'new_contacts' trigger" in info["last_error"]
