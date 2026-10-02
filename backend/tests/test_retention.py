from __future__ import annotations

from sqlalchemy import text

from app.db.session import get_sessionmaker
from app.services.retention import purge_expired
from tests.conftest import drain, publish, run
from tests.factories import linear


async def _age(execution_id: str, days: int) -> None:
    async with get_sessionmaker()() as s:
        await s.execute(
            text("UPDATE executions SET finished_at = now() - make_interval(days => :d) WHERE id = :e"),
            {"d": days, "e": execution_id},
        )
        await s.commit()


async def _exists(execution_id: str) -> bool:
    async with get_sessionmaker()() as s:
        return bool((await s.execute(text("SELECT 1 FROM executions WHERE id = :e"), {"e": execution_id})).first())


async def _purge() -> dict[str, int]:
    async with get_sessionmaker()() as s:
        stats = await purge_expired(s)
        await s.commit()
    return stats


async def test_purges_expired_executions_but_keeps_recent_and_unresolved_failures(client, tenant):
    ok = await publish(client, tenant, linear(("a", "logic.set_variable", {"assignments": {"x": 1}})))
    bad = await publish(client, tenant, linear(("boom", "logic.stop", {"outcome": "error", "message": "x"})))
    old_ok, recent_ok, old_failed = (
        await run(client, tenant, ok),
        await run(client, tenant, ok),
        await run(client, tenant, bad),
    )
    await drain()
    await _age(old_ok, 120)
    await _age(old_failed, 120)  # has an unresolved dead letter: an operator still has to look at it
    await _purge()
    assert not await _exists(old_ok)
    assert await _exists(recent_ok) and await _exists(old_failed)
    async with get_sessionmaker()() as s:  # node runs and events went with the execution (cascade)
        leftovers = (
            await s.execute(text("SELECT count(*) FROM node_runs WHERE execution_id = :e"), {"e": old_ok})
        ).scalar_one()
    assert leftovers == 0
    dl = next(
        d
        for d in (await client.get("/api/v1/dead-letters", headers=tenant.headers)).json()
        if d["execution_id"] == old_failed
    )
    await client.post(f"/api/v1/dead-letters/{dl['id']}/resolve", headers=tenant.headers)
    await _purge()
    assert not await _exists(old_failed)  # resolved: now eligible


async def test_org_override_and_validation(client, tenant):
    wf = await publish(client, tenant, linear(("a", "logic.set_variable", {"assignments": {"x": 1}})))
    ex = await run(client, tenant, wf)
    await drain()
    await _age(ex, 400)
    r = await client.patch(
        "/api/v1/orgs/current", headers=tenant.headers, json={"settings": {"execution_retention_days": -1}}
    )
    assert r.status_code == 422
    r = await client.patch(
        "/api/v1/orgs/current", headers=tenant.headers, json={"settings": {"execution_retention_days": 0}}
    )
    assert r.status_code == 200
    await _purge()
    assert await _exists(ex)  # 0 = keep forever for this organization
    await client.patch(
        "/api/v1/orgs/current", headers=tenant.headers, json={"settings": {"execution_retention_days": 365}}
    )
    await _purge()
    assert not await _exists(ex)


async def test_audit_log_retention(client, tenant):
    async with get_sessionmaker()() as s:
        org_id = (
            await s.execute(text("SELECT org_id FROM workspaces WHERE id = :w"), {"w": tenant.workspace_id})
        ).scalar_one()
        await s.execute(
            text("UPDATE audit_logs SET created_at = now() - interval '400 days' WHERE org_id = :o"), {"o": org_id}
        )
        await s.commit()
    await _purge()
    async with get_sessionmaker()() as s:
        left = (await s.execute(text("SELECT count(*) FROM audit_logs WHERE org_id = :o"), {"o": org_id})).scalar_one()
    assert left == 0
