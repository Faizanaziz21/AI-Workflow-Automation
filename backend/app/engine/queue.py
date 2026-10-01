"""Durable PostgreSQL job queue.

* ``enqueue`` inserts in the caller's transaction, so a state change and the job that continues it commit
  atomically (no dual-write problem). Optional ``dedupe_key`` coalesces identical *queued* jobs.
* ``claim`` uses ``FOR UPDATE SKIP LOCKED`` so many workers pull concurrently without contention, and sets a
  lease (``locked_until``). Workers heartbeat to extend leases while a job runs.
* ``reap_expired`` returns jobs whose lease expired (worker crashed / was killed) to the queue — this is the
  crash-recovery path. Jobs exceeding ``max_attempts`` are moved to the dead-letter table.
* ``pg_notify('flowforge_jobs')`` wakes idle workers immediately; polling is the fallback.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import JobStatus
from app.db.models import DeadLetter, Job

NOTIFY_CHANNEL = "flowforge_jobs"
DEFAULT_QUEUE = "default"


@dataclass
class ClaimedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    org_id: uuid.UUID | None
    execution_id: uuid.UUID | None
    attempts: int
    max_attempts: int
    available_at: datetime
    queue: str


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    *,
    org_id: uuid.UUID | None = None,
    execution_id: uuid.UUID | None = None,
    available_at: datetime | None = None,
    priority: int = 100,
    dedupe_key: str | None = None,
    max_attempts: int = 10,
    queue: str = DEFAULT_QUEUE,
    notify: bool = True,
) -> int | None:
    now = datetime.now(UTC)
    values = {
        "queue": queue,
        "kind": kind,
        "payload": payload,
        "org_id": org_id,
        "execution_id": execution_id,
        "status": JobStatus.QUEUED.value,
        "priority": priority,
        "available_at": available_at or now,
        "dedupe_key": dedupe_key,
        "max_attempts": max_attempts,
        "created_at": now,
        "updated_at": now,
    }
    stmt = insert(Job).values(**values)
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=text("dedupe_key IS NOT NULL AND status = 'queued'"),
        )
    result = await session.execute(stmt.returning(Job.id))
    job_id = result.scalar_one_or_none()
    if job_id is not None and notify and (available_at is None or available_at <= now):
        await session.execute(text("SELECT pg_notify(:ch, :payload)"), {"ch": NOTIFY_CHANNEL, "payload": queue})
    return job_id


async def claim(
    session: AsyncSession, worker_id: str, *, limit: int, lease_seconds: int, queues: list[str] | None = None
) -> list[ClaimedJob]:
    queues = queues or [DEFAULT_QUEUE]
    rows = (
        await session.execute(
            text("""
        UPDATE jobs SET status = 'running', locked_by = :worker,
               locked_until = now() + make_interval(secs => :lease), attempts = attempts + 1, updated_at = now()
        WHERE id IN (
            SELECT id FROM jobs
            WHERE status = 'queued' AND queue = ANY(:queues) AND available_at <= now()
            ORDER BY priority, available_at, id
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, kind, payload, org_id, execution_id, attempts, max_attempts, available_at, queue
    """),
            {"worker": worker_id, "lease": lease_seconds, "queues": queues, "limit": limit},
        )
    ).all()
    return [
        ClaimedJob(
            id=r.id,
            kind=r.kind,
            payload=r.payload if isinstance(r.payload, dict) else json.loads(r.payload),
            org_id=r.org_id,
            execution_id=r.execution_id,
            attempts=r.attempts,
            max_attempts=r.max_attempts,
            available_at=r.available_at,
            queue=r.queue,
        )
        for r in rows
    ]


async def complete(session: AsyncSession, job_id: int, worker_id: str) -> None:
    await session.execute(delete(Job).where(Job.id == job_id, Job.locked_by == worker_id))


async def fail(
    session: AsyncSession,
    job: ClaimedJob,
    worker_id: str,
    error: str,
    *,
    retry_in: float | None = None,
) -> bool:
    """Record a job-level failure. Returns True if dead-lettered."""
    if job.attempts >= job.max_attempts or retry_in is None:
        await session.execute(delete(Job).where(Job.id == job.id, Job.locked_by == worker_id))
        session.add(
            DeadLetter(
                org_id=job.org_id,
                execution_id=job.execution_id,
                source="job",
                kind=job.kind,
                payload=job.payload,
                error=error[:10_000],
                attempts=job.attempts,
            )
        )
        return True
    await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.locked_by == worker_id)
        .values(
            status=JobStatus.QUEUED.value,
            locked_by=None,
            locked_until=None,
            last_error=error[:5000],
            available_at=datetime.now(UTC) + timedelta(seconds=retry_in),
            updated_at=datetime.now(UTC),
        )
    )
    return False


async def heartbeat(session: AsyncSession, job_ids: list[int], worker_id: str, lease_seconds: int) -> None:
    if not job_ids:
        return
    await session.execute(
        update(Job)
        .where(Job.id.in_(job_ids), Job.locked_by == worker_id)
        .values(locked_until=datetime.now(UTC) + timedelta(seconds=lease_seconds))
    )


async def release(session: AsyncSession, job_ids: list[int], worker_id: str) -> None:
    """Graceful shutdown: hand unfinished jobs back without consuming an attempt."""
    if not job_ids:
        return
    await session.execute(
        update(Job)
        .where(Job.id.in_(job_ids), Job.locked_by == worker_id)
        .values(
            status=JobStatus.QUEUED.value,
            locked_by=None,
            locked_until=None,
            attempts=Job.attempts - 1,
            updated_at=datetime.now(UTC),
        )
    )


async def reap_expired(session: AsyncSession) -> int:
    """Return expired leases to the queue (worker crash recovery); dead-letter exhausted jobs."""
    # A queued twin with the same dedupe key already covers the work.
    await session.execute(
        text("""
        DELETE FROM jobs j WHERE j.status = 'running' AND j.locked_until < now() AND j.dedupe_key IS NOT NULL
          AND EXISTS (SELECT 1 FROM jobs q WHERE q.dedupe_key = j.dedupe_key AND q.status = 'queued')
    """)
    )
    exhausted = (
        await session.execute(
            text("""
        DELETE FROM jobs WHERE status = 'running' AND locked_until < now() AND attempts >= max_attempts
        RETURNING org_id, execution_id, kind, payload, attempts, last_error
    """)
        )
    ).all()
    for r in exhausted:
        session.add(
            DeadLetter(
                org_id=r.org_id,
                execution_id=r.execution_id,
                source="job",
                kind=r.kind,
                payload=r.payload,
                attempts=r.attempts,
                error=f"Lease expired after {r.attempts} attempts (worker crash or timeout). "
                f"Last error: {r.last_error or 'none'}",
            )
        )
    result = await session.execute(
        text("""
        UPDATE jobs SET status = 'queued', locked_by = NULL, locked_until = NULL, updated_at = now(),
               last_error = 'lease expired; recovered by reaper'
        WHERE status = 'running' AND locked_until < now()
    """)
    )
    return int(result.rowcount or 0) + len(exhausted)  # type: ignore[attr-defined]


async def depth(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Job.queue, Job.status, func.count()).group_by(Job.queue, Job.status))).all()
    ready = (
        await session.execute(
            select(func.count()).select_from(Job).where(Job.status == "queued", Job.available_at <= func.now())
        )
    ).scalar_one()
    delayed = (
        await session.execute(
            select(func.count()).select_from(Job).where(Job.status == "queued", Job.available_at > func.now())
        )
    ).scalar_one()
    oldest = (
        await session.execute(
            select(func.min(Job.available_at)).where(Job.status == "queued", Job.available_at <= func.now())
        )
    ).scalar_one()
    return {
        "by_status": [{"queue": q, "status": s, "count": c} for q, s, c in rows],
        "ready": ready,
        "delayed": delayed,
        "oldest_ready_age_seconds": (datetime.now(UTC) - oldest).total_seconds() if oldest else 0.0,
    }


async def delete_execution_jobs(session: AsyncSession, execution_id: uuid.UUID, kinds: list[str] | None = None) -> None:
    stmt = delete(Job).where(Job.execution_id == execution_id, Job.status == JobStatus.QUEUED.value)
    if kinds:
        stmt = stmt.where(Job.kind.in_(kinds))
    await session.execute(stmt)


async def delete_by_dedupe(session: AsyncSession, dedupe_key: str) -> None:
    await session.execute(delete(Job).where(Job.dedupe_key == dedupe_key, Job.status == JobStatus.QUEUED.value))
