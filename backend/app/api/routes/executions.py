"""Executions: run, list, inspect (masked), timeline, cancel/pause/resume/retry/replay, signals, dead letters."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import PrincipalDep, SessionDep, WorkspaceDep
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailedError
from app.core.masking import MASK, mask_data, mask_inline
from app.core.principal import Principal
from app.core.rbac import Permission
from app.db.enums import TERMINAL_EXECUTION_STATUSES, ExecutionStatus, TriggerType
from app.db.models import DeadLetter, Execution, ExecutionEvent, NodeRun, Workflow, WorkflowVersion
from app.engine import executor
from app.services import audit
from app.services import workflows as wf_service

router = APIRouter(tags=["executions"])


class RunBody(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=150)
    correlation_key: str | None = Field(default=None, max_length=200)


class ReplayBody(BaseModel):
    scope: str = Field(default="", max_length=500)


class SignalBody(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    payload: Any = None


def _sensitive_ok(principal: Principal, workspace_id: uuid.UUID) -> bool:
    return principal.has(Permission.EXECUTIONS_READ_SENSITIVE, workspace_id)


def _mask(principal: Principal, workspace_id: uuid.UUID, value: Any, extra_keys: list[str] | None = None) -> Any:
    if value is None:
        return None
    out = value if _sensitive_ok(principal, workspace_id) else mask_data(value)
    if extra_keys:
        out = _mask_keys(out, set(extra_keys))
    return out


def _mask_keys(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: (MASK if k in keys and v not in (None, "") else _mask_keys(v, keys)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_keys(v, keys) for v in value]
    return value


def execution_summary(ex: Execution, workflow_name: str | None = None, version: int | None = None) -> dict[str, Any]:
    duration = None
    if ex.started_at:
        end = ex.finished_at or datetime.now(UTC)
        duration = int((end - ex.started_at).total_seconds() * 1000)
    return {
        "id": str(ex.id),
        "workflow_id": str(ex.workflow_id),
        "workflow_name": workflow_name,
        "workflow_version_id": str(ex.workflow_version_id),
        "workflow_version": version,
        "workspace_id": str(ex.workspace_id),
        "status": ex.status,
        "trigger_type": ex.trigger_type,
        "correlation_key": ex.correlation_key,
        "is_paused": ex.is_paused,
        "retry_count": ex.retry_count,
        "created_at": ex.created_at.isoformat(),
        "started_at": ex.started_at.isoformat() if ex.started_at else None,
        "finished_at": ex.finished_at.isoformat() if ex.finished_at else None,
        "duration_ms": duration,
        "error": ex.error,
        "parent_execution_id": str(ex.parent_execution_id) if ex.parent_execution_id else None,
    }


async def _get_execution(
    session: Any, principal: Principal, execution_id: uuid.UUID, *, lock: bool = False
) -> Execution:
    stmt = select(Execution).where(Execution.id == execution_id, Execution.org_id == principal.org_id)
    if lock:
        stmt = stmt.with_for_update()
    ex = (await session.execute(stmt)).scalar_one_or_none()
    if ex is None or not principal.can_access_workspace(ex.workspace_id):
        raise NotFoundError("Execution not found")
    principal.require(Permission.EXECUTIONS_READ, ex.workspace_id)
    return ex


@router.post("/workspaces/{workspace_id}/workflows/{workflow_id}/run", status_code=202)
async def run_workflow(
    workflow_id: uuid.UUID, body: RunBody, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep
) -> dict[str, Any]:
    principal.require(Permission.EXECUTIONS_RUN, ws.id)
    wf = await wf_service.get_workflow(session, principal, workflow_id, ws.id)
    if wf.status != "active":
        raise ConflictError("Workflow is archived")
    if wf.published_version_id is None:
        raise ConflictError("Publish the workflow before running it", code="not_published")
    ex, created = await executor.start_execution(
        session,
        org_id=principal.org_id,
        workspace_id=ws.id,
        workflow_id=wf.id,
        version_id=wf.published_version_id,
        trigger_type=TriggerType.MANUAL.value,
        payload=body.input,
        idempotency_key=f"manual:{body.idempotency_key}" if body.idempotency_key else None,
        correlation_key=body.correlation_key,
        created_by=principal.user_id,
    )
    if created:
        await audit.record(
            session,
            principal,
            "execution.started",
            resource_type="execution",
            resource_id=ex.id,
            workspace_id=ws.id,
            summary=f"Manual run of '{wf.name}'",
        )
    return {"execution_id": str(ex.id), "status": ex.status, "created": created}


@router.get("/executions")
async def list_executions(
    principal: PrincipalDep,
    session: SessionDep,
    workspace_id: uuid.UUID | None = None,
    workflow_id: uuid.UUID | None = None,
    status: Annotated[list[str] | None, Query()] = None,
    trigger_type: str | None = None,
    correlation_key: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: datetime | None = None,
) -> dict[str, Any]:
    allowed = principal.workspaces_with(Permission.EXECUTIONS_READ)
    stmt = (
        select(Execution, Workflow.name, WorkflowVersion.version)
        .join(Workflow, Workflow.id == Execution.workflow_id)
        .join(WorkflowVersion, WorkflowVersion.id == Execution.workflow_version_id)
        .where(Execution.org_id == principal.org_id)
    )
    if allowed is not None:
        stmt = stmt.where(Execution.workspace_id.in_(allowed or [uuid.UUID(int=0)]))
    if workspace_id:
        stmt = stmt.where(Execution.workspace_id == workspace_id)
    if workflow_id:
        stmt = stmt.where(Execution.workflow_id == workflow_id)
    if status:
        stmt = stmt.where(Execution.status.in_([s.upper() for s in status]))
    if trigger_type:
        stmt = stmt.where(Execution.trigger_type == trigger_type)
    if correlation_key:
        stmt = stmt.where(Execution.correlation_key == correlation_key)
    if since:
        stmt = stmt.where(Execution.created_at >= since)
    if until:
        stmt = stmt.where(Execution.created_at <= until)
    count_stmt = select(func.count()).select_from(stmt.with_only_columns(Execution.id).subquery())
    total = (await session.execute(count_stmt)).scalar_one()
    if before:
        stmt = stmt.where(Execution.created_at < before)
    rows = (await session.execute(stmt.order_by(Execution.created_at.desc()).limit(limit))).all()
    items = []
    for ex, name, version in rows:
        item = execution_summary(ex, name, version)
        item["error"] = _mask(principal, ex.workspace_id, ex.error)
        items.append(item)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "next_cursor": items[-1]["created_at"] if len(items) == limit else None,
    }


@router.get("/executions/{execution_id}")
async def get_execution(execution_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    ex = await _get_execution(session, principal, execution_id)
    wf = await session.get(Workflow, ex.workflow_id)
    version = await session.get(WorkflowVersion, ex.workflow_version_id)
    definition = version.definition if version else {"nodes": [], "edges": []}
    mask_fields = definition.get("settings", {}).get("mask_fields", [])
    runs = (
        (
            await session.execute(
                select(NodeRun).where(NodeRun.execution_id == ex.id).order_by(NodeRun.scheduled_at, NodeRun.node_id)
            )
        )
        .scalars()
        .all()
    )
    data = execution_summary(ex, wf.name if wf else None, version.version if version else None)
    data["trigger_payload"] = _mask(principal, ex.workspace_id, ex.trigger_payload, mask_fields)
    data["output"] = _mask(principal, ex.workspace_id, ex.output, mask_fields)
    data["definition"] = {"nodes": definition.get("nodes", []), "edges": definition.get("edges", [])}
    data["node_runs"] = [node_run_out(principal, ex, r, mask_fields, include_data=False) for r in runs]
    data["sensitive_data_visible"] = _sensitive_ok(principal, ex.workspace_id)
    return data


def node_run_out(
    principal: Principal, ex: Execution, r: NodeRun, mask_fields: list[str], *, include_data: bool
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(r.id),
        "node_id": r.node_id,
        "node_type": r.node_type,
        "scope": r.scope,
        "status": r.status,
        "attempt": r.attempt,
        "max_attempts": r.max_attempts,
        "branches": r.branches,
        "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "duration_ms": r.duration_ms,
        "wait_until": r.wait_until.isoformat() if r.wait_until else None,
        "wait_key": r.wait_key,
        "error": _mask(principal, ex.workspace_id, r.error, mask_fields),
        "idempotency_key": r.idempotency_key,
    }
    if include_data:
        out["input"] = _mask(principal, ex.workspace_id, r.input, mask_fields)
        out["output"] = _mask(principal, ex.workspace_id, r.output, mask_fields)
        out["state"] = {k: v for k, v in (r.state or {}).items() if k not in ("loop_items", "resume")}
    return out


@router.get("/executions/{execution_id}/nodes")
async def list_node_runs(
    execution_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, node_id: str | None = None
) -> list[dict[str, Any]]:
    ex = await _get_execution(session, principal, execution_id)
    version = await session.get(WorkflowVersion, ex.workflow_version_id)
    mask_fields = (version.definition if version else {}).get("settings", {}).get("mask_fields", [])
    stmt = select(NodeRun).where(NodeRun.execution_id == ex.id)
    if node_id:
        stmt = stmt.where(NodeRun.node_id == node_id)
    runs = (await session.execute(stmt.order_by(NodeRun.scheduled_at, NodeRun.scope))).scalars().all()
    return [node_run_out(principal, ex, r, mask_fields, include_data=True) for r in runs]


@router.get("/executions/{execution_id}/events")
async def list_events(
    execution_id: uuid.UUID,
    principal: PrincipalDep,
    session: SessionDep,
    after_id: int = 0,
    node_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[dict[str, Any]]:
    ex = await _get_execution(session, principal, execution_id)
    stmt = select(ExecutionEvent).where(ExecutionEvent.execution_id == ex.id, ExecutionEvent.id > after_id)
    if node_id:
        stmt = stmt.where(ExecutionEvent.node_id == node_id)
    rows = (await session.execute(stmt.order_by(ExecutionEvent.id).limit(limit))).scalars().all()
    return [
        {
            "id": e.id,
            "type": e.event_type,
            "level": e.level,
            "message": e.message,
            "node_id": e.node_id,
            "scope": e.scope,
            "data": _mask(principal, ex.workspace_id, e.data),
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ]


async def _operate(session: Any, principal: Principal, execution_id: uuid.UUID) -> Execution:
    ex = await _get_execution(session, principal, execution_id, lock=True)
    principal.require(Permission.EXECUTIONS_OPERATE, ex.workspace_id)
    return ex


@router.post("/executions/{execution_id}/cancel")
async def cancel(execution_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    ex = await _operate(session, principal, execution_id)
    if ExecutionStatus(ex.status) in TERMINAL_EXECUTION_STATUSES:
        raise ConflictError(f"Execution already {ex.status}")
    await executor.cancel_execution(session, ex, f"Cancelled by {principal.email or principal.name}")
    await audit.record(
        session,
        principal,
        "execution.cancelled",
        resource_type="execution",
        resource_id=ex.id,
        workspace_id=ex.workspace_id,
        summary="Execution cancelled",
    )
    return {"status": ex.status}


@router.post("/executions/{execution_id}/pause")
async def pause(execution_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    ex = await _operate(session, principal, execution_id)
    if ExecutionStatus(ex.status) in TERMINAL_EXECUTION_STATUSES:
        raise ConflictError(f"Execution already {ex.status}")
    await executor.pause_execution(session, ex)
    await audit.record(
        session,
        principal,
        "execution.paused",
        resource_type="execution",
        resource_id=ex.id,
        workspace_id=ex.workspace_id,
        summary="Execution paused",
    )
    return {"status": "PAUSING", "is_paused": True}


@router.post("/executions/{execution_id}/resume")
async def resume(execution_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    ex = await _operate(session, principal, execution_id)
    if ex.status == ExecutionStatus.FAILED.value:
        reset = await executor.retry_execution(session, ex)
        action = "execution.retried"
    elif ex.is_paused:
        await executor.resume_execution(session, ex)
        reset, action = 0, "execution.resumed"
    else:
        raise ConflictError("Execution is neither paused nor failed")
    await audit.record(
        session,
        principal,
        action,
        resource_type="execution",
        resource_id=ex.id,
        workspace_id=ex.workspace_id,
        summary="Execution resumed",
        details={"nodes_reset": reset},
    )
    return {"status": ex.status, "nodes_reset": reset}


@router.post("/executions/{execution_id}/retry")
async def retry(execution_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    ex = await _operate(session, principal, execution_id)
    if ex.status not in (ExecutionStatus.FAILED.value, ExecutionStatus.CANCELLED.value):
        raise ConflictError("Only failed or cancelled executions can be retried")
    reset = await executor.retry_execution(session, ex)
    await audit.record(
        session,
        principal,
        "execution.retried",
        resource_type="execution",
        resource_id=ex.id,
        workspace_id=ex.workspace_id,
        summary=f"Partial retry ({reset} nodes)",
        details={"nodes_reset": reset},
    )
    return {"status": ex.status, "nodes_reset": reset}


@router.post("/executions/{execution_id}/nodes/{node_id}/replay")
async def replay(
    execution_id: uuid.UUID, node_id: str, body: ReplayBody, principal: PrincipalDep, session: SessionDep
) -> dict[str, Any]:
    ex = await _operate(session, principal, execution_id)
    if ExecutionStatus(ex.status) not in TERMINAL_EXECUTION_STATUSES:
        raise ConflictError("Replay is available once the execution has finished; cancel or pause it first")
    try:
        removed = await executor.replay_node(session, ex, node_id, body.scope)
    except (LookupError, ValueError) as exc:
        raise ValidationFailedError(str(exc)) from exc
    await audit.record(
        session,
        principal,
        "execution.node_replayed",
        resource_type="execution",
        resource_id=ex.id,
        workspace_id=ex.workspace_id,
        summary=f"Replayed node '{node_id}'",
        details={"node_id": node_id, "scope": body.scope, "runs_reset": removed},
    )
    return {"status": ex.status, "runs_reset": removed}


@router.post("/executions/{execution_id}/signals")
async def signal_execution(
    execution_id: uuid.UUID, body: SignalBody, principal: PrincipalDep, session: SessionDep
) -> dict[str, Any]:
    ex = await _operate(session, principal, execution_id)
    resumed = await executor.signal(session, ex.org_id, body.key, body.payload, ex.id)
    return {"resumed": resumed}


@router.post("/signals")
async def signal_by_key(body: SignalBody, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    """Resume every node in the organisation waiting on ``key`` (e.g. ``reply:jane@acme.com``)."""
    allowed = principal.workspaces_with(Permission.EVENTS_PUBLISH)
    if allowed is not None and not allowed:
        raise PermissionDeniedError("Missing permission 'events:publish'")
    resumed = await executor.signal(session, principal.org_id, body.key, body.payload, workspace_ids=allowed)
    return {"resumed": resumed}


# ----------------------------------------------------------------------------- dead letters


@router.get("/dead-letters")
async def list_dead_letters(
    principal: PrincipalDep,
    session: SessionDep,
    include_resolved: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    allowed = principal.workspaces_with(Permission.DEAD_LETTERS_MANAGE)
    if allowed is not None and not allowed:
        raise PermissionDeniedError("Missing permission 'dead_letters:manage'")
    stmt = (
        select(DeadLetter, Execution.workspace_id, Workflow.name)
        .outerjoin(Execution, Execution.id == DeadLetter.execution_id)
        .outerjoin(Workflow, Workflow.id == Execution.workflow_id)
        .where(DeadLetter.org_id == principal.org_id)
    )
    if allowed is not None:  # workspace-scoped operators only see their workspaces' dead letters
        stmt = stmt.where(Execution.workspace_id.in_(allowed))
    if not include_resolved:
        stmt = stmt.where(DeadLetter.resolved_at.is_(None))
    rows = (await session.execute(stmt.order_by(DeadLetter.created_at.desc()).limit(limit))).all()
    return [
        {
            "id": str(d.id),
            "execution_id": str(d.execution_id) if d.execution_id else None,
            "workspace_id": str(ws_id) if ws_id else None,
            "workflow_name": wf_name,
            "node_id": d.node_id,
            "source": d.source,
            "kind": d.kind,
            "error": mask_inline(d.error or ""),
            "attempts": d.attempts,
            "created_at": d.created_at.isoformat(),
            "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
            "resolution": d.resolution,
        }
        for d, ws_id, wf_name in rows
    ]


async def _dead_letter(session: Any, principal: Principal, dl_id: uuid.UUID) -> DeadLetter:
    dl = (
        await session.execute(
            select(DeadLetter).where(DeadLetter.id == dl_id, DeadLetter.org_id == principal.org_id).with_for_update()
        )
    ).scalar_one_or_none()
    if dl is None:
        raise NotFoundError("Dead letter not found")
    if dl.execution_id is not None:
        ws_id = (await session.execute(select(Execution.workspace_id).where(Execution.id == dl.execution_id))).scalar()
        if ws_id is None or not principal.can_access_workspace(ws_id):
            raise NotFoundError("Dead letter not found")
        principal.require(Permission.DEAD_LETTERS_MANAGE, ws_id)
    else:
        principal.require(Permission.DEAD_LETTERS_MANAGE)  # org-level jobs (e.g. trigger polls): org-wide role
    return dl


@router.post("/dead-letters/{dl_id}/requeue")
async def requeue_dead_letter(dl_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    dl = await _dead_letter(session, principal, dl_id)
    if dl.resolved_at:
        raise ConflictError("Already resolved")
    result: dict[str, Any] = {}
    if dl.source == "node" and dl.execution_id:
        ex = await _operate(session, principal, dl.execution_id)
        if ex.status not in (ExecutionStatus.FAILED.value, ExecutionStatus.CANCELLED.value):
            raise ConflictError(f"Execution is {ex.status}")
        result["nodes_reset"] = await executor.retry_execution(session, ex)
    else:
        from app.engine import queue

        await queue.enqueue(session, dl.kind, dl.payload, org_id=dl.org_id, execution_id=dl.execution_id)
    dl.resolved_at = datetime.now(UTC)
    dl.resolved_by = principal.user_id
    dl.resolution = "requeued"
    await audit.record(
        session,
        principal,
        "dead_letter.requeued",
        resource_type="dead_letter",
        resource_id=dl.id,
        summary=f"Dead letter {dl.kind} requeued",
    )
    return {"status": "requeued", **result}


@router.post("/dead-letters/{dl_id}/resolve")
async def resolve_dead_letter(dl_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    dl = await _dead_letter(session, principal, dl_id)
    dl.resolved_at = datetime.now(UTC)
    dl.resolved_by = principal.user_id
    dl.resolution = "dismissed"
    await audit.record(
        session,
        principal,
        "dead_letter.resolved",
        resource_type="dead_letter",
        resource_id=dl.id,
        summary="Dead letter dismissed",
    )
    return {"status": "dismissed"}


@router.get("/workspaces/{workspace_id}/queue")
async def queue_stats(ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    principal.require(Permission.DASHBOARD_READ, ws.id)
    from app.engine import queue

    return await queue.depth(session)
