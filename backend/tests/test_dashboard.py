from __future__ import annotations

import httpx
import respx

from tests.conftest import add_user, drain, publish, run
from tests.factories import linear


@respx.mock
async def test_dashboard_kpis(client, make_tenant):
    tenant = await make_tenant()
    respx.get("https://api.example.com/ok").mock(return_value=httpx.Response(200, json={}))
    respx.get("https://api.example.com/bad").mock(return_value=httpx.Response(400))
    ok = await publish(
        client, tenant, linear(("call", "data.http_request", {"url": "https://api.example.com/ok"})), "ok"
    )
    bad = await publish(
        client, tenant, linear(("call", "data.http_request", {"url": "https://api.example.com/bad"})), "bad"
    )
    for _ in range(3):
        await run(client, tenant, ok)
    await run(client, tenant, bad)
    await drain()
    s = (await client.get("/api/v1/dashboard/summary", headers=tenant.headers, params={"window": "1h"})).json()
    ex = s["executions"]
    assert ex["started"] == 4 and ex["completed"] == 3 and ex["failed"] == 1
    assert ex["failure_rate"] == 0.25 and ex["avg_duration_ms"] >= 0
    assert s["integrations"]["failures"] == 1 and s["integrations"]["calls"] == 4
    assert {"ready", "scheduled", "in_flight"} <= set(s["queue"])
    ts = (await client.get("/api/v1/dashboard/timeseries", headers=tenant.headers, params={"window": "1h"})).json()
    assert sum(p["started"] for p in ts["executions"]) == 4 and ts["bucket"] == "minute"
    wfs = (await client.get("/api/v1/dashboard/workflows", headers=tenant.headers, params={"window": "1h"})).json()
    assert {w["name"]: w["failed"] for w in wfs} == {"ok": 0, "bad": 1}
    nodes = (await client.get("/api/v1/dashboard/nodes", headers=tenant.headers, params={"window": "1h"})).json()
    assert nodes[0]["node_type"] == "data.http_request" and nodes[0]["runs"] == 4


async def test_dashboard_scoping(client, make_tenant):
    a = await make_tenant()
    r = await client.post("/api/v1/workspaces", headers=a.headers, json={"name": "Other WS"})
    other_ws = r.json()["id"]
    scoped = await add_user(client, a, "viewer", workspace_id=other_ws)
    assert (
        await client.get(
            "/api/v1/dashboard/summary", headers=scoped["headers"], params={"workspace_id": a.workspace_id}
        )
    ).status_code == 404
    assert (await client.get("/api/v1/dashboard/summary", headers=scoped["headers"])).status_code == 200
    b = await make_tenant()
    s = (await client.get("/api/v1/dashboard/summary", headers=b.headers)).json()
    assert s["executions"]["started"] == 0


async def test_prometheus_metrics_endpoint(client):
    r = await client.get("/metrics")
    assert r.status_code == 200
    for metric in (
        "flowforge_http_requests_total",
        "flowforge_executions_started_total",
        "flowforge_node_runs_total",
        "flowforge_ai_calls_total",
        "flowforge_queue_depth",
    ):
        assert metric in r.text
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"] == {"database": "ok", "redis": "ok"}
