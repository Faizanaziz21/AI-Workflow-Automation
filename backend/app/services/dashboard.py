"""Operational dashboard aggregates (tenant- and workspace-scoped, time-bounded, index-backed)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AICall, ApprovalRequest, Execution, ExecutionEvent, Job, NodeRun, Workflow

WINDOWS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
BUCKETS = {"15m": "minute", "1h": "minute", "6h": "minute", "24h": "hour", "7d": "hour", "30d": "day"}
ACTIVE = ("PENDING", "RUNNING", "RETRYING")
WAITING = ("WAITING", "WAITING_APPROVAL", "PAUSED")
INTEGRATION_FILTER = and_(
    NodeRun.node_type.notlike("logic.%"),
    NodeRun.node_type.notlike("trigger.%"),
    NodeRun.node_type.notlike("human.%"),
    NodeRun.node_type.notlike("ai.%"),
)


def _scope(
    stmt: Any, model: Any, org_id: uuid.UUID, workspaces: set[uuid.UUID] | None, workspace_id: uuid.UUID | None
) -> Any:
    stmt = stmt.where(model.org_id == org_id)
    if workspace_id is not None:
        stmt = stmt.where(model.workspace_id == workspace_id)
    elif workspaces is not None:
        stmt = stmt.where(model.workspace_id.in_(workspaces or [uuid.UUID(int=0)]))
    return stmt


async def summary(
    session: AsyncSession,
    org_id: uuid.UUID,
    workspaces: set[uuid.UUID] | None,
    workspace_id: uuid.UUID | None,
    window: str,
) -> dict[str, Any]:
    since = datetime.now(UTC) - WINDOWS[window]
    minutes = WINDOWS[window].total_seconds() / 60

    live = (
        await session.execute(
            _scope(
                select(
                    func.count().filter(Execution.status.in_(ACTIVE)).label("running"),
                    func.count().filter(Execution.status.in_(WAITING)).label("waiting"),
                    func.count().filter(Execution.status == "WAITING_APPROVAL").label("waiting_approval"),
                ),
                Execution,
                org_id,
                workspaces,
                workspace_id,
            ).where(Execution.status.notin_(["COMPLETED", "FAILED", "CANCELLED"]))
        )
    ).one()

    win = (
        await session.execute(
            _scope(
                select(
                    func.count().label("started"),
                    func.count().filter(Execution.status == "COMPLETED").label("completed"),
                    func.count().filter(Execution.status == "FAILED").label("failed"),
                    func.count().filter(Execution.status == "CANCELLED").label("cancelled"),
                    func.avg(func.extract("epoch", Execution.finished_at - Execution.started_at) * 1000)
                    .filter(Execution.status == "COMPLETED")
                    .label("avg_ms"),
                    func.percentile_cont(0.95)
                    .within_group(func.extract("epoch", Execution.finished_at - Execution.started_at) * 1000)
                    .filter(Execution.status == "COMPLETED")
                    .label("p95_ms"),
                    func.sum(Execution.retry_count).label("manual_retries"),
                ),
                Execution,
                org_id,
                workspaces,
                workspace_id,
            ).where(Execution.created_at >= since)
        )
    ).one()
    finished = (win.completed or 0) + (win.failed or 0)

    ai_stmt = select(
        func.count().label("calls"),
        func.coalesce(func.sum(AICall.cost_usd), 0).label("cost"),
        func.coalesce(func.sum(AICall.input_tokens), 0).label("tin"),
        func.coalesce(func.sum(AICall.output_tokens), 0).label("tout"),
        func.count().filter(AICall.status != "success").label("errors"),
        func.count().filter(AICall.is_fallback.is_(True)).label("fallbacks"),
    ).where(AICall.org_id == org_id, AICall.created_at >= since)
    if workspace_id is not None or workspaces is not None:
        ai_stmt = _scope(
            ai_stmt.join(Execution, Execution.id == AICall.execution_id), Execution, org_id, workspaces, workspace_id
        )
    ai = (await session.execute(ai_stmt)).one()

    nr_base = select(NodeRun).join(Execution, Execution.id == NodeRun.execution_id)
    integ = (
        await session.execute(
            _scope(
                nr_base.with_only_columns(
                    func.count().filter(NodeRun.status == "FAILED").label("failed"),
                    func.count().filter(NodeRun.error.is_not(None)).label("errored"),
                    func.count().label("calls"),
                ),
                Execution,
                org_id,
                workspaces,
                workspace_id,
            ).where(NodeRun.finished_at >= since, INTEGRATION_FILTER)
        )
    ).one()

    ev_stmt = (
        select(func.count()).select_from(ExecutionEvent).join(Execution, Execution.id == ExecutionEvent.execution_id)
    )
    retries = (
        await session.execute(
            _scope(ev_stmt, Execution, org_id, workspaces, workspace_id).where(
                ExecutionEvent.created_at >= since, ExecutionEvent.event_type == "node_retry_scheduled"
            )
        )
    ).scalar_one()

    queue = (
        await session.execute(
            select(
                func.count().filter(and_(Job.status == "queued", Job.available_at <= func.now())).label("ready"),
                func.count().filter(and_(Job.status == "queued", Job.available_at > func.now())).label("scheduled"),
                func.count().filter(Job.status == "running").label("running"),
                func.min(Job.available_at)
                .filter(and_(Job.status == "queued", Job.available_at <= func.now()))
                .label("oldest"),
            ).where(Job.org_id == org_id)
        )
    ).one()

    approvals_pending = (
        await session.execute(
            _scope(
                select(func.count()).select_from(ApprovalRequest), ApprovalRequest, org_id, workspaces, workspace_id
            ).where(ApprovalRequest.status == "pending")
        )
    ).scalar_one()

    return {
        "window": window,
        "since": since.isoformat(),
        "executions": {
            "running": live.running,
            "waiting": live.waiting,
            "waiting_approval": live.waiting_approval,
            "started": win.started,
            "completed": win.completed,
            "failed": win.failed,
            "cancelled": win.cancelled,
            "failure_rate": round((win.failed or 0) / finished, 4) if finished else 0.0,
            "avg_duration_ms": round(float(win.avg_ms or 0)),
            "p95_duration_ms": round(float(win.p95_ms or 0)),
            "per_minute": round((win.started or 0) / minutes, 2),
            "manual_retries": int(win.manual_retries or 0),
        },
        "queue": {
            "ready": queue.ready,
            "scheduled": queue.scheduled,
            "in_flight": queue.running,
            "oldest_ready_age_seconds": round((datetime.now(UTC) - queue.oldest).total_seconds(), 1)
            if queue.oldest
            else 0.0,
        },
        "ai": {
            "calls": ai.calls,
            "errors": ai.errors,
            "fallbacks": ai.fallbacks,
            "cost_usd": round(float(ai.cost), 6),
            "input_tokens": int(ai.tin),
            "output_tokens": int(ai.tout),
            "total_tokens": int(ai.tin) + int(ai.tout),
        },
        "integrations": {"calls": integ.calls, "failures": integ.failed, "errored_attempts": integ.errored},
        "retries": {"scheduled": retries},
        "approvals": {"pending": approvals_pending},
    }


async def timeseries(
    session: AsyncSession,
    org_id: uuid.UUID,
    workspaces: set[uuid.UUID] | None,
    workspace_id: uuid.UUID | None,
    window: str,
) -> dict[str, Any]:
    since = datetime.now(UTC) - WINDOWS[window]
    unit = BUCKETS[window]
    bucket = func.date_trunc(unit, Execution.created_at).label("bucket")
    rows = (
        await session.execute(
            _scope(
                select(
                    bucket,
                    func.count().label("started"),
                    func.count().filter(Execution.status == "COMPLETED").label("completed"),
                    func.count().filter(Execution.status == "FAILED").label("failed"),
                    func.avg(func.extract("epoch", Execution.finished_at - Execution.started_at) * 1000)
                    .filter(Execution.status == "COMPLETED")
                    .label("avg_ms"),
                ),
                Execution,
                org_id,
                workspaces,
                workspace_id,
            )
            .where(Execution.created_at >= since)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()
    ai_bucket = func.date_trunc(unit, AICall.created_at).label("bucket")
    ai_rows = (
        await session.execute(
            select(
                ai_bucket,
                func.count().label("calls"),
                func.sum(AICall.cost_usd).label("cost"),
                func.sum(AICall.input_tokens + AICall.output_tokens).label("tokens"),
            )
            .where(AICall.org_id == org_id, AICall.created_at >= since)
            .group_by(ai_bucket)
            .order_by(ai_bucket)
        )
    ).all()
    return {
        "window": window,
        "bucket": unit,
        "executions": [
            {
                "t": r.bucket.isoformat(),
                "started": r.started,
                "completed": r.completed,
                "failed": r.failed,
                "avg_duration_ms": round(float(r.avg_ms or 0)),
            }
            for r in rows
        ],
        "ai": [
            {"t": r.bucket.isoformat(), "calls": r.calls, "cost_usd": float(r.cost or 0), "tokens": int(r.tokens or 0)}
            for r in ai_rows
        ],
    }


async def workflow_stats(
    session: AsyncSession,
    org_id: uuid.UUID,
    workspaces: set[uuid.UUID] | None,
    workspace_id: uuid.UUID | None,
    window: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - WINDOWS[window]
    failed = func.count().filter(Execution.status == "FAILED")
    total = func.count()
    rows = (
        await session.execute(
            _scope(
                select(
                    Execution.workflow_id,
                    Workflow.name,
                    total.label("runs"),
                    func.count().filter(Execution.status == "COMPLETED").label("completed"),
                    failed.label("failed"),
                    func.avg(func.extract("epoch", Execution.finished_at - Execution.started_at) * 1000)
                    .filter(Execution.status == "COMPLETED")
                    .label("avg_ms"),
                    func.max(Execution.created_at).label("last_run"),
                ).join(Workflow, Workflow.id == Execution.workflow_id),
                Execution,
                org_id,
                workspaces,
                workspace_id,
            )
            .where(Execution.created_at >= since)
            .group_by(Execution.workflow_id, Workflow.name)
            .order_by(total.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "workflow_id": str(r.workflow_id),
            "name": r.name,
            "runs": r.runs,
            "completed": r.completed,
            "failed": r.failed,
            "failure_rate": round(r.failed / r.runs, 4) if r.runs else 0.0,
            "avg_duration_ms": round(float(r.avg_ms or 0)),
            "last_run": r.last_run.isoformat(),
        }
        for r in rows
    ]


async def node_stats(
    session: AsyncSession,
    org_id: uuid.UUID,
    workspaces: set[uuid.UUID] | None,
    workspace_id: uuid.UUID | None,
    window: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - WINDOWS[window]
    runs = func.count()
    rows = (
        await session.execute(
            _scope(
                select(
                    NodeRun.node_type,
                    runs.label("runs"),
                    func.count().filter(NodeRun.status == "FAILED").label("failed"),
                    func.sum(case((NodeRun.attempt > 1, NodeRun.attempt - 1), else_=0)).label("retries"),
                    func.avg(NodeRun.duration_ms).label("avg_ms"),
                    func.percentile_cont(0.95).within_group(NodeRun.duration_ms).label("p95_ms"),
                ).join(Execution, Execution.id == NodeRun.execution_id),
                Execution,
                org_id,
                workspaces,
                workspace_id,
            )
            .where(
                NodeRun.finished_at >= since,
                NodeRun.status.in_(["COMPLETED", "FAILED"]),
                NodeRun.node_type.notlike("trigger.%"),
            )
            .group_by(NodeRun.node_type)
            .order_by(literal_column("p95_ms").desc().nulls_last())
            .limit(limit)
        )
    ).all()
    return [
        {
            "node_type": r.node_type,
            "runs": r.runs,
            "failed": r.failed,
            "retries": int(r.retries or 0),
            "avg_duration_ms": round(float(r.avg_ms or 0)),
            "p95_duration_ms": round(float(r.p95_ms or 0)),
        }
        for r in rows
    ]
