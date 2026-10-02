"""Data retention: bounded, batched purges run by the scheduler.

* **Executions**: terminal executions (``COMPLETED``/``FAILED``/``CANCELLED``) finished more than
  ``execution_retention_days`` ago are deleted together with their node runs, events and approval requests
  (foreign-key cascades), queued jobs and resolved dead letters. Executions with an *unresolved* dead letter are
  kept until an operator requeues or dismisses it.
* **Audit log and AI usage ledger**: rows older than ``audit_retention_days`` are deleted.

Defaults come from ``FF_EXECUTION_RETENTION_DAYS`` / ``FF_AUDIT_RETENTION_DAYS``; an organization overrides them
with ``settings.execution_retention_days`` / ``settings.audit_retention_days`` (``PATCH /orgs/current``). ``0`` keeps
data forever. Each run deletes at most ``max_batches × batch_size`` rows per category so purges never hold long
locks or starve the engine.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _days(setting: str) -> str:
    """SQL for the effective retention of ``o`` (org override when it is a non-negative integer, else default)."""
    return (
        f"CASE WHEN (o.settings->>'{setting}') ~ '^[0-9]+$' THEN (o.settings->>'{setting}')::int ELSE :default_days END"
    )


_PURGE_EXECUTIONS = text(f"""
WITH doomed AS (
    SELECT e.id FROM executions e JOIN organizations o ON o.id = e.org_id
    WHERE e.status IN ('COMPLETED', 'FAILED', 'CANCELLED')
      AND e.finished_at IS NOT NULL
      AND {_days("execution_retention_days")} > 0
      AND e.finished_at < now() - make_interval(days => {_days("execution_retention_days")})
      AND NOT EXISTS (SELECT 1 FROM dead_letters d WHERE d.execution_id = e.id AND d.resolved_at IS NULL)
    ORDER BY e.finished_at
    LIMIT :batch
    FOR UPDATE OF e SKIP LOCKED
), purged_dead_letters AS (
    DELETE FROM dead_letters d USING doomed WHERE d.execution_id = doomed.id
), purged_jobs AS (
    DELETE FROM jobs j USING doomed WHERE j.execution_id = doomed.id
)
DELETE FROM executions e USING doomed WHERE e.id = doomed.id
""")


def _purge_by_created(table: str) -> Any:
    return text(f"""
DELETE FROM {table} WHERE id IN (
    SELECT t.id FROM {table} t JOIN organizations o ON o.id = t.org_id
    WHERE {_days("audit_retention_days")} > 0
      AND t.created_at < now() - make_interval(days => {_days("audit_retention_days")})
    LIMIT :batch
)
""")


_PURGE_AUDIT = _purge_by_created("audit_logs")
_PURGE_AI_CALLS = _purge_by_created("ai_calls")


async def purge_expired(session: AsyncSession, *, batch_size: int = 500, max_batches: int = 20) -> dict[str, int]:
    """Delete expired data in batches; the caller commits. Returns rows deleted per category."""
    settings = get_settings()
    jobs = [
        ("executions", _PURGE_EXECUTIONS, settings.execution_retention_days),
        ("audit_logs", _PURGE_AUDIT, settings.audit_retention_days),
        ("ai_calls", _PURGE_AI_CALLS, settings.audit_retention_days),
    ]
    stats: dict[str, int] = {}
    for name, stmt, default_days in jobs:
        total = 0
        for _ in range(max_batches):
            result = await session.execute(stmt, {"default_days": default_days, "batch": batch_size})
            deleted = int(getattr(result, "rowcount", 0) or 0)
            total += deleted
            if deleted < batch_size:
                break
        stats[name] = total
    if any(stats.values()):
        logger.info("retention purge", extra={"deleted": stats})
    return stats
