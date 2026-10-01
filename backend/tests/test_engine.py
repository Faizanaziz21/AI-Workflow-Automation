"""Workflow engine behaviour: control flow, durability, retries, waits, operator actions."""

from __future__ import annotations

import asyncio

import httpx
import respx
from sqlalchemy import select, text, update

from app.db.models import DeadLetter, Job, NodeRun
from app.db.session import get_sessionmaker
from app.engine import queue
from tests.conftest import drain, fast_forward, get_execution, node_runs, publish, run
from tests.factories import definition, edge, linear, node


def setvar(node_id: str, **assignments) -> dict:
    return node(node_id, "logic.set_variable", {"assignments": assignments})


async def test_linear_execution_with_expressions(client, tenant):
    d = linear(
        ("a", "logic.set_variable", {"assignments": {"total": "{{ trigger.qty * trigger.price }}"}}),
        (
            "b",
            "data.json_transform",
            {"template": {"label": "Order of {{ vars.total }}", "big": "{{ vars.total > 100 }}"}},
        ),
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf, {"qty": 3, "price": 50})
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "COMPLETED", ex
    assert ex["output"]["b"] == {"label": "Order of 150", "big": True}
    runs = await node_runs(client, tenant, ex_id)
    assert runs["a"]["output"] == {"total": 150}
    assert runs["b"]["input"]["config"]["template"]["label"].startswith("Order")
    events = (await client.get(f"/api/v1/executions/{ex_id}/events", headers=tenant.headers)).json()
    types = [e["type"] for e in events]
    assert types[0] == "execution_created" and "node_completed" in types and types[-1] == "execution_completed"


async def test_jobs_complete_in_the_state_transaction(client, tenant):
    """Advance and node jobs are deleted in the transaction that commits their state change: nothing is left
    claimed or queued once an execution finishes."""
    wf = await publish(client, tenant, linear(("a", "logic.set_variable", {"assignments": {"x": 1}})))
    ex_id = await run(client, tenant, wf, {})
    await drain()
    assert (await get_execution(client, tenant, ex_id))["status"] == "COMPLETED"
    async with get_sessionmaker()() as session:
        left = (await session.execute(select(Job.kind, Job.status).where(Job.execution_id == ex_id))).all()
    assert left == []


async def test_if_branching_skip_propagation_and_merge(client, tenant):
    d = definition(
        [
            node("t", "trigger.manual"),
            node("check", "logic.if", {"condition": "{{ trigger.amount > 5000 }}"}),
            setvar("big", tier="'big'"),
            setvar("small", tier="small"),
            setvar("after_small", x=1),
            node("merge", "logic.merge", {"mode": "object"}),
        ],
        [
            edge("t", "check"),
            edge("check", "big", "true"),
            edge("check", "small", "false"),
            edge("small", "after_small"),
            edge("big", "merge"),
            edge("after_small", "merge"),
        ],
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf, {"amount": 8000})
    await drain()
    runs = await node_runs(client, tenant, ex_id)
    assert runs["check"]["branches"] == ["true"]
    assert runs["big"]["status"] == "COMPLETED"
    assert runs["small"]["status"] == "SKIPPED" and runs["after_small"]["status"] == "SKIPPED"
    assert runs["merge"]["status"] == "COMPLETED"
    assert list(runs["merge"]["output"].keys()) == ["big"]
    assert (await get_execution(client, tenant, ex_id))["status"] == "COMPLETED"


async def test_switch_routing(client, tenant):
    d = definition(
        [
            node("t", "trigger.manual"),
            node(
                "sw",
                "logic.switch",
                {
                    "value": "{{ trigger.kind }}",
                    "cases": [{"handle": "sales", "value": "sales"}, {"handle": "support", "value": "support"}],
                },
            ),
            setvar("s1", r="sales"),
            setvar("s2", r="support"),
            setvar("s3", r="default"),
        ],
        [edge("t", "sw"), edge("sw", "s1", "sales"), edge("sw", "s2", "support"), edge("sw", "s3", "default")],
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf, {"kind": "billing"})
    await drain()
    runs = await node_runs(client, tenant, ex_id)
    assert [k for k, v in runs.items() if v["status"] == "COMPLETED"] == ["t", "sw", "s3"]


async def test_loop_with_scoped_iterations(client, tenant):
    d = definition(
        [
            node("t", "trigger.manual"),
            node("loop", "logic.loop", {"items": "{{ trigger.leads }}", "max_concurrency": 2}),
            node(
                "score",
                "logic.set_variable",
                {"assignments": {"email": "{{ loop.item.email }}", "score": "{{ loop.item.size * 10 }}"}},
            ),
            node(
                "label",
                "data.json_transform",
                {
                    "template": {
                        "who": "{{ nodes.score.output.email }}",
                        "hot": "{{ nodes.score.output.score >= 500 }}",
                        "i": "{{ loop.index }}",
                    }
                },
            ),
            node(
                "summary",
                "data.json_transform",
                {
                    "template": {
                        "n": "{{ nodes.loop.output.count }}",
                        "hot": "{{ [r.who for r in nodes.loop.output.results if r.hot] }}",
                    }
                },
            ),
        ],
        [edge("t", "loop"), edge("loop", "score", "body"), edge("score", "label"), edge("loop", "summary", "done")],
    )
    wf = await publish(client, tenant, d)
    leads = [{"email": f"u{i}@x.com", "size": i * 20} for i in range(5)]
    ex_id = await run(client, tenant, wf, {"leads": leads})
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "COMPLETED", ex
    runs = await node_runs(client, tenant, ex_id)
    assert runs["loop"]["output"]["count"] == 5
    assert runs["label@loop:3"]["output"] == {"who": "u3@x.com", "hot": True, "i": 3}
    assert ex["output"]["summary"] == {"n": 5, "hot": ["u3@x.com", "u4@x.com"]}


async def test_empty_loop_completes(client, tenant):
    d = definition(
        [
            node("t", "trigger.manual"),
            node("loop", "logic.loop", {"items": "{{ trigger.items }}"}),
            setvar("body", x=1),
            setvar("after", done=True),
        ],
        [edge("t", "loop"), edge("loop", "body", "body"), edge("loop", "after", "done")],
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf, {"items": []})
    await drain()
    runs = await node_runs(client, tenant, ex_id)
    assert runs["loop"]["output"] == {"count": 0, "results": []}
    assert runs["after"]["status"] == "COMPLETED"


@respx.mock
async def test_retry_with_backoff_then_success(client, tenant):
    route = respx.get("https://api.example.com/flaky")
    route.side_effect = [httpx.Response(503), httpx.Response(503), httpx.Response(200, json={"ok": True})]
    d = linear(("call", "data.http_request", {"url": "https://api.example.com/flaky"}))
    d["nodes"][1]["retry"] = {"max_attempts": 3, "initial_interval_seconds": 30, "backoff_coefficient": 2}
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf)
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "RETRYING"
    async with get_sessionmaker()() as s:
        job = (await s.execute(select(Job).where(Job.execution_id == ex_id, Job.kind == "node.run"))).scalar_one()
        delay = (job.available_at - job.created_at).total_seconds()
        assert 14 <= delay <= 46  # 30s * jitter[0.5, 1.5)
    for _ in range(2):
        await fast_forward(ex_id)
        await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "COMPLETED"
    runs = await node_runs(client, tenant, ex_id)
    assert runs["call"]["attempt"] == 3 and runs["call"]["output"]["body"] == {"ok": True}
    assert route.call_count == 3
    # The same idempotency key is sent on every attempt.
    events = (await client.get(f"/api/v1/executions/{ex_id}/events", headers=tenant.headers)).json()
    assert sum(1 for e in events if e["type"] == "node_retry_scheduled") == 2


@respx.mock
async def test_failure_dead_letter_and_partial_retry(client, tenant):
    first = respx.get("https://api.example.com/first").mock(return_value=httpx.Response(200, json={"step": 1}))
    second = respx.post("https://api.example.com/second")
    second.side_effect = [httpx.Response(400, json={"message": "bad input"}), httpx.Response(200, json={"step": 2})]
    d = linear(
        ("one", "data.http_request", {"url": "https://api.example.com/first"}),
        (
            "two",
            "data.http_request",
            {
                "url": "https://api.example.com/second",
                "method": "POST",
                "body": {"from": "{{ nodes.one.output.body.step }}"},
            },
        ),
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf)
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "FAILED"
    assert "HTTP 400" in ex["error"]["message"] and ex["error"]["node_id"] == "two"
    dls = (await client.get("/api/v1/dead-letters", headers=tenant.headers)).json()
    dl = next(d for d in dls if d["execution_id"] == ex_id)
    r = await client.post(f"/api/v1/dead-letters/{dl['id']}/requeue", headers=tenant.headers)
    assert r.status_code == 200 and r.json()["nodes_reset"] == 1
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "COMPLETED" and ex["retry_count"] == 1
    assert first.call_count == 1  # completed checkpoint was not re-executed
    assert second.call_count == 2
    assert respx.calls[-1].request.headers["idempotency-key"].startswith("ff-")


@respx.mock
async def test_on_error_continue_and_route(client, tenant):
    respx.get("https://api.example.com/broken").mock(return_value=httpx.Response(404))
    d = definition(
        [
            node("t", "trigger.manual"),
            node("cont", "data.http_request", {"url": "https://api.example.com/broken"}, on_error="continue"),
            node("route", "data.http_request", {"url": "https://api.example.com/broken"}, on_error="route"),
            setvar("ok_path", x=1),
            setvar("err_path", msg="{{ nodes.route.output.error.message }}"),
        ],
        [edge("t", "cont"), edge("cont", "route"), edge("route", "ok_path"), edge("route", "err_path", "error")],
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf)
    await drain()
    runs = await node_runs(client, tenant, ex_id)
    assert runs["cont"]["status"] == "COMPLETED" and "error" in runs["cont"]["output"]
    assert runs["ok_path"]["status"] == "SKIPPED"
    assert "404" in runs["err_path"]["output"]["msg"]
    assert (await get_execution(client, tenant, ex_id))["status"] == "COMPLETED"


@respx.mock
async def test_node_timeout(client, tenant):
    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200)

    respx.get("https://api.example.com/slow").mock(side_effect=slow)
    d = linear(("slow", "data.http_request", {"url": "https://api.example.com/slow"}))
    d["nodes"][1]["timeout_seconds"] = 0.3
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf)
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "FAILED"
    assert ex["error"]["code"] == "timeout"


async def test_durable_delay(client, tenant):
    d = linear(
        ("wait", "logic.delay", {"amount": 2, "unit": "days"}),
        ("after", "logic.set_variable", {"assignments": {"done": True}}),
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf)
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "WAITING"
    runs = await node_runs(client, tenant, ex_id)
    assert runs["wait"]["status"] == "WAITING" and runs["wait"]["wait_until"]
    await fast_forward(ex_id)
    await drain()
    assert (await get_execution(client, tenant, ex_id))["status"] == "COMPLETED"


async def test_wait_for_event_signal_and_timeout(client, tenant):
    d = definition(
        [
            node("t", "trigger.manual"),
            node(
                "wait",
                "logic.wait_until",
                {"mode": "event", "event_key": "reply:{{ trigger.email }}", "timeout": 3, "timeout_unit": "days"},
            ),
            setvar("replied", body="{{ nodes.wait.output.payload.text }}"),
            setvar("escalate", x=1),
        ],
        [edge("t", "wait"), edge("wait", "replied", "received"), edge("wait", "escalate", "timeout")],
    )
    wf = await publish(client, tenant, d)
    ex1 = await run(client, tenant, wf, {"email": "jane@acme.com"})
    ex2 = await run(client, tenant, wf, {"email": "bob@acme.com"})
    await drain()
    assert (await get_execution(client, tenant, ex1))["status"] == "WAITING"
    r = await client.post(
        "/api/v1/signals",
        headers=tenant.headers,
        json={"key": "reply:jane@acme.com", "payload": {"text": "Interested!"}},
    )
    assert r.json() == {"resumed": 1}
    r = await client.post("/api/v1/signals", headers=tenant.headers, json={"key": "reply:jane@acme.com"})
    assert r.json() == {"resumed": 0}
    await drain()
    runs1 = await node_runs(client, tenant, ex1)
    assert runs1["replied"]["output"] == {"body": "Interested!"} and runs1["escalate"]["status"] == "SKIPPED"
    await fast_forward(ex2)
    await drain()
    runs2 = await node_runs(client, tenant, ex2)
    assert runs2["wait"]["branches"] == ["timeout"] and runs2["escalate"]["status"] == "COMPLETED"


async def test_cancel_pause_resume(client, tenant):
    d = linear(
        ("wait", "logic.delay", {"amount": 1, "unit": "hours"}),
        ("after", "logic.set_variable", {"assignments": {"a": 1}}),
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf)
    await drain()
    r = await client.post(f"/api/v1/executions/{ex_id}/cancel", headers=tenant.headers)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    runs = await node_runs(client, tenant, ex_id)
    assert runs["wait"]["status"] == "CANCELLED"
    async with get_sessionmaker()() as s:
        remaining = (await s.execute(select(Job).where(Job.execution_id == ex_id))).scalars().all()
        assert remaining == []
    assert (await client.post(f"/api/v1/executions/{ex_id}/cancel", headers=tenant.headers)).status_code == 409

    ex2 = await run(client, tenant, wf)
    await client.post(f"/api/v1/executions/{ex2}/pause", headers=tenant.headers)
    await drain()
    assert (await get_execution(client, tenant, ex2))["status"] == "PAUSED"
    runs = await node_runs(client, tenant, ex2)
    assert "wait" not in runs  # nothing scheduled while paused
    await client.post(f"/api/v1/executions/{ex2}/resume", headers=tenant.headers)
    await drain()
    assert (await get_execution(client, tenant, ex2))["status"] == "WAITING"


async def test_crash_recovery_via_lease_expiry(client, tenant):
    """A worker claims a node job and dies; the reaper re-queues it and another worker finishes the run."""
    from app.workers.scheduler import Scheduler

    d = linear(("a", "logic.set_variable", {"assignments": {"v": "{{ trigger.v }}"}}))
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf, {"v": 42})
    # Process only the first advance (schedules node 'a'), leaving node.run queued.
    async with get_sessionmaker()() as s:
        jobs = await queue.claim(s, "crashy", limit=10, lease_seconds=60)
        await s.commit()
    advance_job = next(j for j in jobs if j.kind == "execution.advance" and str(j.execution_id) == ex_id)
    from app.engine import executor

    async with get_sessionmaker()() as s:
        await executor.advance(s, advance_job.execution_id)
        await queue.complete(s, advance_job.id, "crashy")
        await s.execute(update(Job).where(Job.locked_by == "crashy").values(status="queued", locked_by=None))
        await s.commit()
    # "crashy" worker claims the node job, marks the node RUNNING, then dies (no completion).
    async with get_sessionmaker()() as s:
        claimed = await queue.claim(s, "crashy", limit=10, lease_seconds=60)
        node_job = next(j for j in claimed if j.kind == "node.run" and str(j.execution_id) == ex_id)
        await s.execute(
            update(NodeRun)
            .where(NodeRun.execution_id == ex_id, NodeRun.node_id == "a")
            .values(status="RUNNING", worker_id="crashy", attempt=1)
        )
        await s.execute(
            update(Job).where(Job.locked_by == "crashy", Job.id != node_job.id).values(status="queued", locked_by=None)
        )
        await s.commit()
    await drain()  # job is leased by the dead worker: nothing happens
    assert (await node_runs(client, tenant, ex_id))["a"]["status"] == "RUNNING"
    async with get_sessionmaker()() as s:
        await s.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE locked_by = 'crashy'"))
        await s.commit()
    stats = await Scheduler().tick()
    assert stats["reaped"] >= 1
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "COMPLETED"
    runs = await node_runs(client, tenant, ex_id)
    assert runs["a"]["output"] == {"v": 42} and runs["a"]["attempt"] == 2
    events = (await client.get(f"/api/v1/executions/{ex_id}/events", headers=tenant.headers)).json()
    assert any(e["type"] == "node_recovered" for e in events)


async def test_poison_job_goes_to_dead_letter(client, tenant):
    async with get_sessionmaker()() as s:
        await queue.enqueue(s, "unknown.kind", {"x": 1}, max_attempts=1)
        await s.commit()
    await drain()
    async with get_sessionmaker()() as s:
        dl = (await s.execute(select(DeadLetter).where(DeadLetter.kind == "unknown.kind"))).scalars().all()
        assert dl and "No handler" in dl[-1].error


async def test_idempotent_runs(client, tenant):
    wf = await publish(client, tenant, linear(("a", "logic.set_variable", {"assignments": {"x": 1}})))
    a = await run(client, tenant, wf, idempotency_key="order-123")
    b = await run(client, tenant, wf, idempotency_key="order-123")
    c = await run(client, tenant, wf, idempotency_key="order-124")
    assert a == b != c


async def test_replay_node_reexecutes_downstream(client, tenant):
    d = linear(
        ("a", "logic.set_variable", {"assignments": {"x": 1}}),
        ("b", "data.json_transform", {"template": {"at": "{{ now() }}"}}),
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf)
    await drain()
    before = (await node_runs(client, tenant, ex_id))["b"]
    r = await client.post(f"/api/v1/executions/{ex_id}/nodes/a/replay", headers=tenant.headers, json={})
    assert r.status_code == 200 and r.json()["runs_reset"] == 2
    await drain()
    after = await node_runs(client, tenant, ex_id)
    assert after["b"]["output"]["at"] != before["output"]["at"]
    assert (await get_execution(client, tenant, ex_id))["status"] == "COMPLETED"


async def test_execution_timeout(client, tenant):
    d = linear(("wait", "logic.delay", {"amount": 1, "unit": "days"}))
    d["settings"] = {"execution_timeout_seconds": 60}
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf)
    await drain()
    async with get_sessionmaker()() as s:
        await s.execute(
            text("UPDATE executions SET deadline_at = now() - interval '1 second' WHERE id = :e"), {"e": ex_id}
        )
        await s.execute(
            text("UPDATE jobs SET available_at = now() WHERE execution_id = :e AND kind = 'execution.timeout'"),
            {"e": ex_id},
        )
        await s.commit()
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "FAILED" and ex["error"]["code"] == "execution_timeout"


async def test_sub_workflow(client, tenant):
    child = await publish(
        client,
        tenant,
        linear(("double", "logic.set_variable", {"assignments": {"result": "{{ trigger.n * 2 }}"}})),
        name="child",
    )
    parent = await publish(
        client,
        tenant,
        linear(
            ("call", "logic.sub_workflow", {"workflow_id": child, "input": {"n": "{{ trigger.n }}"}}),
            ("use", "logic.set_variable", {"assignments": {"got": "{{ nodes.call.output.output.double.result }}"}}),
        ),
        name="parent",
    )
    ex_id = await run(client, tenant, parent, {"n": 21})
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "COMPLETED", ex
    assert (await node_runs(client, tenant, ex_id))["use"]["output"] == {"got": 42}


async def test_execution_isolation(client, tenant, make_tenant):
    wf = await publish(client, tenant, linear(("a", "logic.set_variable", {"assignments": {"x": 1}})))
    ex_id = await run(client, tenant, wf)
    other = await make_tenant()
    assert (await client.get(f"/api/v1/executions/{ex_id}", headers=other.headers)).status_code == 404
    assert (await client.post(f"/api/v1/executions/{ex_id}/cancel", headers=other.headers)).status_code == 404
    listing = (await client.get("/api/v1/executions", headers=other.headers)).json()
    assert all(i["id"] != ex_id for i in listing["items"])
    r = await client.post(other.ws(f"/workflows/{wf}/run"), headers=other.headers, json={"input": {}})
    assert r.status_code == 404


async def test_dedupe_enqueue_blocks_claim_until_commit():
    """Lost-wakeup regression: coalescing onto a queued job must keep it unclaimable until the enqueuing
    transaction commits, otherwise a worker can run it against state that is not yet visible."""
    sm = get_sessionmaker()
    q = "race-test"
    async with sm() as s:
        await queue.enqueue(s, "execution.advance", {"n": 1}, dedupe_key="race:1", queue=q)
        await s.commit()
    writer = sm()
    try:
        await queue.enqueue(writer, "execution.advance", {"n": 1}, dedupe_key="race:1", queue=q)  # not committed
        async with sm() as s:
            assert await queue.claim(s, "w-race", limit=5, lease_seconds=30, queues=[q]) == []
            await s.rollback()
        await writer.commit()
    finally:
        await writer.close()
    async with sm() as s:
        claimed = await queue.claim(s, "w-race", limit=5, lease_seconds=30, queues=[q])
        assert len(claimed) == 1  # coalesced: still exactly one job
        for job in claimed:
            await queue.complete(s, job.id, "w-race")
        await s.commit()
