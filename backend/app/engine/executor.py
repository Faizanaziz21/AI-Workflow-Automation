"""Execution state machine.

``advance`` is the heart of the engine. It is invoked (via a deduplicated ``execution.advance`` job) whenever
something about an execution changes. Holding a row lock on the execution, it:

1. fails the execution if a node failed terminally (and cancels everything still pending);
2. progresses Loop nodes (starts iterations up to their concurrency, completes them when all iterations end);
3. computes the *frontier* per scope — nodes whose incoming edges are all resolved — and either schedules
   them (at least one active edge) or marks them SKIPPED (dead-path elimination, which propagates);
4. derives the aggregate execution status, or completes the execution.

Everything is recomputed from committed ``node_runs`` (the checkpoints), so advance is idempotent and a
crash at any point is recovered by simply running it again.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import OrderedDict, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached

from app.core.config import get_settings
from app.core.metrics import EXECUTION_DURATION, EXECUTIONS_FINISHED, EXECUTIONS_STARTED
from app.core.redis import get_redis
from app.db.enums import (
    TERMINAL_NODE_STATUSES,
    ApprovalStatus,
    ExecutionStatus,
    NodeRunStatus,
)
from app.db.models import ApprovalRequest, DeadLetter, Execution, ExecutionEvent, NodeRun, WorkflowVersion
from app.engine import queue
from app.engine.definition import RetryPolicy, WorkflowDefinition
from app.engine.graph import LOOP_BODY_HANDLE, LOOP_TYPE, Graph, parent_scope, scope_for

logger = logging.getLogger(__name__)

JOB_ADVANCE = "execution.advance"
JOB_NODE = "node.run"
JOB_TIMEOUT = "execution.timeout"
JOB_POLL = "trigger.poll"

S = NodeRunStatus
E = ExecutionStatus

_def_cache: OrderedDict[uuid.UUID, tuple[WorkflowDefinition, Graph]] = OrderedDict()


def now() -> datetime:
    return datetime.now(UTC)


async def load_definition(session: AsyncSession, version_id: uuid.UUID) -> tuple[WorkflowDefinition, Graph]:
    """Published versions are immutable, so parsed definitions are cached by version id."""
    cached = _def_cache.get(version_id)
    if cached is not None:
        _def_cache.move_to_end(version_id)
        return cached
    version = await session.get(WorkflowVersion, version_id)
    if version is None:
        raise LookupError(f"Workflow version {version_id} not found")
    definition = WorkflowDefinition.model_validate(version.definition)
    entry = (definition, Graph.build(definition))
    if version.status != "draft":
        _def_cache[version_id] = entry
        if len(_def_cache) > 512:
            _def_cache.popitem(last=False)
    return entry


def add_event(
    session: AsyncSession,
    ex: Execution,
    event_type: str,
    message: str = "",
    *,
    node_id: str | None = None,
    scope: str | None = None,
    level: str = "info",
    data: dict[str, Any] | None = None,
) -> None:
    session.add(
        ExecutionEvent(
            org_id=ex.org_id,
            execution_id=ex.id,
            node_id=node_id,
            scope=scope or None,
            event_type=event_type,
            level=level,
            message=message[:4000],
            data=data or {},
        )
    )


def retry_policy_for(definition: WorkflowDefinition, node_id: str) -> RetryPolicy:
    from app.nodes.registry import get_node_type

    node = definition.node(node_id)
    if node.retry is not None:
        return node.retry
    nt = get_node_type(node.type)
    if nt.default_retry is not None and definition.settings.default_retry.max_attempts == 1:
        return nt.default_retry
    return definition.settings.default_retry


def idempotency_key_for(execution_id: uuid.UUID, node_id: str, scope: str) -> str:
    return f"ff-{execution_id.hex[:16]}-{node_id}" + (f"-{scope.replace('/', '.').replace(':', '_')}" if scope else "")


# ----------------------------------------------------------------------------- creation


_INSERT_EXECUTION = (
    pg_insert(Execution)
    .values({c.key: bindparam(c.key, type_=c.type) for c in Execution.__table__.columns})
    .on_conflict_do_nothing(index_elements=["workflow_id", "idempotency_key"])
    .returning(Execution.id)
)


async def start_execution(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
    trigger_type: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
    correlation_key: str | None = None,
    created_by: uuid.UUID | None = None,
    parent_execution_id: uuid.UUID | None = None,
    priority: int = 0,
) -> tuple[Execution, bool]:
    """Create an execution (idempotent on ``(workflow_id, idempotency_key)``). Returns (execution, created)."""
    definition, graph = await load_definition(session, version_id)
    trigger_ids = graph.trigger_ids()
    if not trigger_ids:
        raise ValueError("Workflow version has no trigger node")
    settings = get_settings()
    timeout = definition.settings.execution_timeout_seconds or settings.default_execution_timeout_seconds
    created = now()
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "org_id": org_id,
        "workspace_id": workspace_id,
        "workflow_id": workflow_id,
        "workflow_version_id": version_id,
        "status": E.RUNNING.value,
        "trigger_type": trigger_type,
        "trigger_payload": payload,
        "idempotency_key": idempotency_key,
        "correlation_key": (correlation_key or None) and str(correlation_key)[:200],
        "parent_execution_id": parent_execution_id,
        "priority": priority,
        "is_paused": False,
        "cancel_requested": False,
        "error": None,
        "output": None,
        "retry_count": 0,
        "deadline_at": created + timedelta(seconds=timeout),
        "created_by": created_by,
        "created_at": created,
        "started_at": created,
        "finished_at": None,
        "updated_at": created,
    }
    # One round trip: a duplicate idempotency key inserts nothing (instead of a pre-check plus a savepoint).
    inserted = (await session.execute(_INSERT_EXECUTION, fields)).scalar_one_or_none()
    if inserted is None:
        existing = (
            await session.execute(
                select(Execution).where(
                    Execution.workflow_id == workflow_id, Execution.idempotency_key == idempotency_key
                )
            )
        ).scalar_one()
        return existing, False
    ex = Execution(**fields)
    make_transient_to_detached(ex)  # the row exists; attach it to the session without a second INSERT
    session.add(ex)
    trigger_id = trigger_ids[0]
    session.add(
        NodeRun(
            org_id=org_id,
            execution_id=ex.id,
            node_id=trigger_id,
            node_type=definition.node(trigger_id).type,
            scope="",
            status=S.COMPLETED.value,
            attempt=1,
            max_attempts=1,
            output=payload,
            branches=["out"],
            idempotency_key=idempotency_key_for(ex.id, trigger_id, ""),
            scheduled_at=created,
            started_at=created,
            finished_at=created,
            duration_ms=0,
        )
    )
    add_event(
        session,
        ex,
        "execution_created",
        f"Triggered by {trigger_type}",
        node_id=trigger_id,
        data={"idempotency_key": idempotency_key} if idempotency_key else None,
    )
    await enqueue_advance(session, ex)
    await queue.enqueue(
        session,
        JOB_TIMEOUT,
        {"execution_id": str(ex.id)},
        org_id=org_id,
        execution_id=ex.id,
        available_at=ex.deadline_at,
        dedupe_key=f"timeout:{ex.id}",
        notify=False,
    )
    EXECUTIONS_STARTED.labels(trigger_type).inc()
    return ex, True


async def enqueue_advance(session: AsyncSession, ex: Execution, delay: float = 0) -> None:
    await queue.enqueue(
        session,
        JOB_ADVANCE,
        {"execution_id": str(ex.id)},
        org_id=ex.org_id,
        execution_id=ex.id,
        dedupe_key=f"advance:{ex.id}",
        priority=50 - min(ex.priority, 40),
        available_at=now() + timedelta(seconds=delay) if delay else None,
    )


# ----------------------------------------------------------------------------- advance


def _branches(run: NodeRun) -> list[str]:
    return list(run.branches) if run.branches is not None else ["out"]


def _iteration_results(graph: Graph, loop_id: str, scope: str, index: dict[tuple[str, str], NodeRun]) -> Any:
    terminals = graph.body_terminals(loop_id)
    outputs = {
        t: index[(t, scope)].output
        for t in terminals
        if (t, scope) in index and index[(t, scope)].status == S.COMPLETED.value
    }
    if len(terminals) == 1:
        return outputs.get(terminals[0])
    return outputs


async def advance(session: AsyncSession, execution_id: uuid.UUID) -> None:
    ex = (
        await session.execute(select(Execution).where(Execution.id == execution_id).with_for_update())
    ).scalar_one_or_none()
    if ex is None or ExecutionStatus(ex.status).is_terminal:
        return
    definition, graph = await load_definition(session, ex.workflow_version_id)
    runs = list((await session.execute(select(NodeRun).where(NodeRun.execution_id == ex.id))).scalars().all())

    failed = [r for r in runs if r.status == S.FAILED.value]
    if failed:
        await fail_execution(session, ex, runs, failed[0].error or {"message": "Node failed"}, failed[0].node_id)
        return

    index: dict[tuple[str, str], NodeRun] = {(r.node_id, r.scope): r for r in runs}
    inflight = sum(1 for r in runs if r.status in (S.SCHEDULED.value, S.RUNNING.value))
    budget = definition.settings.max_parallel_nodes - inflight
    to_schedule: list[NodeRun] = []

    def new_run(node_id: str, scope: str, status: S) -> NodeRun:
        node_def = definition.node(node_id)
        policy = retry_policy_for(definition, node_id)
        run = NodeRun(
            id=uuid.uuid4(),
            org_id=ex.org_id,
            execution_id=ex.id,
            node_id=node_id,
            node_type=node_def.type,
            scope=scope,
            status=status.value,
            attempt=0,
            max_attempts=policy.max_attempts,
            idempotency_key=idempotency_key_for(ex.id, node_id, scope),
            scheduled_at=now(),
            state={},
        )
        if status == S.SKIPPED:
            run.finished_at = run.scheduled_at
        session.add(run)
        runs.append(run)
        index[(node_id, scope)] = run
        return run

    if not ex.is_paused:
        changed = True
        while changed:
            changed = False
            # (a) Loops: start / complete iterations.
            for loop_run in [
                r
                for r in runs
                if r.node_type == LOOP_TYPE and r.status == S.WAITING.value and "loop_items" in (r.state or {})
            ]:
                state = dict(loop_run.state)
                items = state["loop_items"]
                started = int(state.get("next", 0))
                concurrency = int(state.get("max_concurrency", 5))
                done_count = 0
                for i in range(started):
                    it_scope = scope_for(loop_run.scope, loop_run.node_id, i)
                    body = graph.direct_body(loop_run.node_id)
                    if all(
                        (n, it_scope) in index and index[(n, it_scope)].status in TERMINAL_NODE_STATUSES for n in body
                    ):
                        done_count += 1
                running_iterations = started - done_count
                while started < len(items) and running_iterations < concurrency:
                    started += 1
                    running_iterations += 1
                    changed = True
                if started != state.get("next"):
                    state["next"] = started
                    loop_run.state = state
                if done_count == len(items) and started == len(items):
                    results = [
                        _iteration_results(
                            graph, loop_run.node_id, scope_for(loop_run.scope, loop_run.node_id, i), index
                        )
                        for i in range(len(items))
                    ]
                    loop_run.status = S.COMPLETED.value
                    loop_run.output = {"count": len(items), "results": results}
                    loop_run.branches = ["done"]
                    loop_run.finished_at = now()
                    if loop_run.started_at:
                        loop_run.duration_ms = int((loop_run.finished_at - loop_run.started_at).total_seconds() * 1000)
                    loop_run.state = state | {"item_count": len(items)}
                    add_event(
                        session,
                        ex,
                        "node_completed",
                        f"Loop finished {len(items)} iterations",
                        node_id=loop_run.node_id,
                        scope=loop_run.scope,
                    )
                    changed = True

            # (b) Frontier per active scope.
            scopes: list[tuple[str, str | None]] = [("", None)]
            for r in runs:
                if r.node_type == LOOP_TYPE and r.status == S.WAITING.value and "loop_items" in (r.state or {}):
                    for i in range(int(r.state.get("next", 0))):
                        scopes.append((scope_for(r.scope, r.node_id, i), r.node_id))
            for scope, level_loop in scopes:
                for node_id in graph.node_ids:
                    if graph.parent_loop.get(node_id) != level_loop or (node_id, scope) in index:
                        continue
                    incoming = graph.incoming[node_id]
                    resolved, active = True, False
                    for e in incoming:
                        if level_loop is not None and e.source == level_loop and e.source_handle == LOOP_BODY_HANDLE:
                            active = True
                            continue
                        src = index.get((e.source, scope))
                        if src is None or src.status not in TERMINAL_NODE_STATUSES:
                            resolved = False
                            break
                        if src.status == S.COMPLETED.value and e.source_handle in _branches(src):
                            active = True
                    if not resolved:
                        continue
                    if not active:
                        new_run(node_id, scope, S.SKIPPED)
                        changed = True
                    elif budget > 0:
                        to_schedule.append(new_run(node_id, scope, S.SCHEDULED))
                        budget -= 1
                        changed = True

    await session.flush()
    for run in to_schedule:
        await queue.enqueue(
            session,
            JOB_NODE,
            {"node_run_id": str(run.id)},
            org_id=ex.org_id,
            execution_id=ex.id,
            priority=100 - min(ex.priority, 90),
            dedupe_key=f"node:{run.id}",
        )

    statuses = [r.status for r in runs]
    non_terminal = [s for s in statuses if s not in TERMINAL_NODE_STATUSES]
    if not non_terminal and not ex.is_paused:
        await finish_execution(session, ex, E.COMPLETED, output=_execution_output(graph, index))
        return
    if any(s in (S.RUNNING.value, S.SCHEDULED.value) for s in non_terminal):
        new_status = E.RUNNING
    elif ex.is_paused:
        new_status = E.PAUSED
    elif S.RETRYING.value in non_terminal:
        new_status = E.RETRYING
    elif S.WAITING_APPROVAL.value in non_terminal:
        new_status = E.WAITING_APPROVAL
    elif S.WAITING.value in non_terminal:
        # A loop that is only waiting on its own iterations counts as running.
        loop_only = all(r.node_type == LOOP_TYPE for r in runs if r.status == S.WAITING.value)
        new_status = E.RUNNING if loop_only else E.WAITING
    else:
        new_status = E.RUNNING
    if ex.status != new_status.value:
        ex.status = new_status.value
        ex.updated_at = now()


def _execution_output(graph: Graph, index: dict[tuple[str, str], NodeRun]) -> dict[str, Any]:
    sinks = [n for n in graph.node_ids if not graph.outgoing[n] and graph.parent_loop.get(n) is None]
    out = {n: index[(n, "")].output for n in sinks if (n, "") in index and index[(n, "")].status == S.COMPLETED.value}
    if len(json.dumps(out, default=str)) > 256_000:
        return {"truncated": True, "nodes": sorted(out)}
    return out


# ----------------------------------------------------------------------------- terminal transitions


async def finish_execution(
    session: AsyncSession, ex: Execution, status: E, *, output: Any = None, error: dict[str, Any] | None = None
) -> None:
    ex.status = status.value
    ex.finished_at = now()
    ex.updated_at = ex.finished_at
    if output is not None:
        ex.output = output
    if error is not None:
        ex.error = error
    await queue.delete_execution_jobs(session, ex.id)
    await session.execute(
        update(ApprovalRequest)
        .where(
            ApprovalRequest.execution_id == ex.id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
        .values(status=ApprovalStatus.CANCELLED.value, updated_at=now())
    )
    add_event(
        session,
        ex,
        f"execution_{status.value.lower()}",
        (error or {}).get("message", "") if error else f"Execution {status.value.lower()}",
        level="error" if status == E.FAILED else "info",
    )
    EXECUTIONS_FINISHED.labels(status.value).inc()
    if ex.started_at:
        EXECUTION_DURATION.observe((ex.finished_at - ex.started_at).total_seconds())
    if ex.parent_execution_id:
        await signal(
            session,
            ex.org_id,
            f"child:{ex.id}",
            {"execution_id": str(ex.id), "status": status.value, "output": ex.output, "error": ex.error},
        )
    if ex.trigger_type == "webhook":
        await push_webhook_response(
            ex.id,
            {
                "status_code": 200 if status == E.COMPLETED else 500,
                "body": {
                    "execution_id": str(ex.id),
                    "status": status.value,
                    **({"output": ex.output} if status == E.COMPLETED else {"error": (error or {}).get("message")}),
                },
                "headers": {},
                "default": True,
            },
        )


async def fail_execution(
    session: AsyncSession, ex: Execution, runs: list[NodeRun], error: dict[str, Any], node_id: str | None
) -> None:
    for r in runs:
        if r.status not in TERMINAL_NODE_STATUSES:
            r.status = S.CANCELLED.value
            r.finished_at = now()
    await finish_execution(session, ex, E.FAILED, error={**error, "node_id": node_id})
    session.add(
        DeadLetter(
            org_id=ex.org_id,
            execution_id=ex.id,
            node_id=node_id,
            source="node",
            kind="execution.failed",
            attempts=ex.retry_count + 1,
            payload={"workflow_id": str(ex.workflow_id), "node_id": node_id},
            error=str(error.get("message", "Node failed"))[:10_000],
        )
    )


async def push_webhook_response(execution_id: uuid.UUID, response: dict[str, Any]) -> None:
    """Hand the synchronous webhook caller its response (first response wins)."""
    try:
        r = get_redis()
        key = f"webhook:response:{execution_id}"
        if response.get("default") and await r.exists(f"{key}:sent"):
            return
        pipe = r.pipeline()
        pipe.rpush(key, json.dumps(response, default=str))
        pipe.expire(key, get_settings().webhook_sync_timeout_seconds + 30)
        pipe.set(f"{key}:sent", "1", ex=get_settings().webhook_sync_timeout_seconds + 30)
        await pipe.execute()
    except Exception:  # Redis is an accelerator here; the caller times out gracefully.
        logger.warning("could not push webhook response", exc_info=True)


# ----------------------------------------------------------------------------- operator actions


async def recover_stalled(session: AsyncSession, *, idle_seconds: int = 60, limit: int = 200) -> int:
    """Self-healing sweep: re-advance active executions that have no pending work.

    An execution that is RUNNING/RETRYING, not paused, has no queued or running job besides its deadline timer
    and no node activity for ``idle_seconds`` cannot make progress on its own. ``advance`` is idempotent, so
    enqueueing one is always safe; it either schedules the missing work or finishes the execution.
    """
    rows = (
        await session.execute(
            text("""
        SELECT e.id FROM executions e
        WHERE e.status IN ('RUNNING', 'RETRYING') AND NOT e.is_paused AND NOT e.cancel_requested
          AND e.created_at < now() - make_interval(secs => :idle)
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.execution_id = e.id AND j.kind <> :timeout_kind)
          AND NOT EXISTS (
              SELECT 1 FROM node_runs n WHERE n.execution_id = e.id
                AND (n.status IN ('SCHEDULED', 'RUNNING')
                     OR coalesce(n.finished_at, n.started_at, n.scheduled_at) > now() - make_interval(secs => :idle)))
        LIMIT :limit
    """),
            {"idle": idle_seconds, "timeout_kind": JOB_TIMEOUT, "limit": limit},
        )
    ).all()
    for (execution_id,) in rows:
        ex = await session.get(Execution, execution_id)
        if ex is None:
            continue
        add_event(session, ex, "execution_recovered", "Re-advanced by the stalled-execution sweep", level="warning")
        await enqueue_advance(session, ex)
    return len(rows)


async def lock_execution(session: AsyncSession, org_id: uuid.UUID, execution_id: uuid.UUID) -> Execution | None:
    return (
        await session.execute(
            select(Execution).where(Execution.id == execution_id, Execution.org_id == org_id).with_for_update()
        )
    ).scalar_one_or_none()


async def cancel_execution(session: AsyncSession, ex: Execution, reason: str = "Cancelled by operator") -> None:
    ex.cancel_requested = True
    runs = list((await session.execute(select(NodeRun).where(NodeRun.execution_id == ex.id))).scalars().all())
    for r in runs:
        if r.status not in TERMINAL_NODE_STATUSES:
            r.status = S.CANCELLED.value
            r.finished_at = now()
    await finish_execution(session, ex, E.CANCELLED, error={"message": reason, "code": "cancelled"})


async def pause_execution(session: AsyncSession, ex: Execution) -> None:
    ex.is_paused = True
    add_event(session, ex, "execution_paused", "Paused: no new nodes will be scheduled")
    await enqueue_advance(session, ex)


async def resume_execution(session: AsyncSession, ex: Execution) -> None:
    ex.is_paused = False
    add_event(session, ex, "execution_resumed", "Resumed")
    await enqueue_advance(session, ex)


async def _reopen(session: AsyncSession, ex: Execution) -> None:
    ex.status = E.RUNNING.value
    ex.finished_at = None
    ex.error = None
    ex.output = None
    ex.cancel_requested = False
    ex.retry_count += 1
    if ex.deadline_at and ex.deadline_at <= now():
        ex.deadline_at = now() + timedelta(seconds=get_settings().default_execution_timeout_seconds)
    await queue.enqueue(
        session,
        JOB_TIMEOUT,
        {"execution_id": str(ex.id)},
        org_id=ex.org_id,
        execution_id=ex.id,
        available_at=ex.deadline_at,
        dedupe_key=f"timeout:{ex.id}",
        notify=False,
    )
    await enqueue_advance(session, ex)


async def retry_execution(session: AsyncSession, ex: Execution) -> int:
    """Partial retry: keep completed checkpoints, re-run failed/cancelled nodes. Returns nodes reset."""
    runs = list((await session.execute(select(NodeRun).where(NodeRun.execution_id == ex.id))).scalars().all())
    reset = 0
    for r in runs:
        if r.status in (S.FAILED.value, S.CANCELLED.value):
            if r.node_type == LOOP_TYPE and "loop_items" in (r.state or {}):
                r.status = S.WAITING.value
                r.finished_at = None
            else:
                await session.delete(r)
            reset += 1
    await session.execute(
        update(DeadLetter)
        .where(DeadLetter.execution_id == ex.id, DeadLetter.resolved_at.is_(None))
        .values(resolved_at=now(), resolution="retried")
    )
    add_event(session, ex, "execution_retried", f"Manual retry #{ex.retry_count + 1}: {reset} node(s) re-queued")
    await _reopen(session, ex)
    return reset


async def replay_node(session: AsyncSession, ex: Execution, node_id: str, scope: str = "") -> int:
    """Re-run a node (and everything downstream of it) of a finished execution using its stored inputs."""
    definition, graph = await load_definition(session, ex.workflow_version_id)
    if node_id not in graph.node_ids:
        raise LookupError(f"Node '{node_id}' not found")
    if definition.node(node_id).type.startswith("trigger."):
        raise ValueError("Trigger nodes cannot be replayed; start a new execution instead")
    runs = list((await session.execute(select(NodeRun).where(NodeRun.execution_id == ex.id))).scalars().all())
    targets = {node_id} | graph.descendants(node_id)

    def in_scope(run_scope: str, base: str) -> bool:
        return run_scope == base or (not base) or run_scope.startswith(base + "/")

    deleted: set[uuid.UUID] = set()

    async def drop(r: NodeRun) -> None:
        if r.id not in deleted:
            deleted.add(r.id)
            await session.delete(r)

    for r in runs:
        if r.node_id in targets and in_scope(r.scope, scope):
            await drop(r)
    # Re-open enclosing loops (and invalidate what came after them) when replaying inside an iteration.
    current = scope
    while current:
        loop_id = current.rsplit("/", 1)[-1].split(":")[0]
        outer = parent_scope(current)
        loop_run = next((r for r in runs if r.node_id == loop_id and r.scope == outer), None)
        if loop_run is not None:
            loop_run.status = S.WAITING.value
            loop_run.finished_at = None
            loop_run.branches = None
            loop_run.output = None
            after_loop = graph.descendants(loop_id) - graph.loop_bodies.get(loop_id, set())
            for r in runs:
                if r.node_id in after_loop and in_scope(r.scope, outer):
                    await drop(r)
        current = outer
    add_event(
        session,
        ex,
        "node_replayed",
        f"Replaying '{node_id}' and {len(targets) - 1} downstream node(s)",
        node_id=node_id,
        scope=scope,
    )
    await _reopen(session, ex)
    return len(deleted)


async def handle_timeout(session: AsyncSession, execution_id: uuid.UUID) -> None:
    ex = (
        await session.execute(select(Execution).where(Execution.id == execution_id).with_for_update())
    ).scalar_one_or_none()
    if ex is None or ExecutionStatus(ex.status).is_terminal:
        return
    if ex.deadline_at and ex.deadline_at > now() + timedelta(seconds=1):
        await queue.enqueue(
            session,
            JOB_TIMEOUT,
            {"execution_id": str(ex.id)},
            org_id=ex.org_id,
            execution_id=ex.id,
            available_at=ex.deadline_at,
            dedupe_key=f"timeout:{ex.id}",
            notify=False,
        )
        return
    runs = list((await session.execute(select(NodeRun).where(NodeRun.execution_id == ex.id))).scalars().all())
    await fail_execution(
        session, ex, runs, {"message": "Execution exceeded its timeout", "code": "execution_timeout"}, None
    )


# ----------------------------------------------------------------------------- waits & signals


async def resume_node(session: AsyncSession, run: NodeRun, reason: str, payload: Any = None) -> None:
    """Re-enter a waiting node (signal, approval decision). Pending timers for the wait are dropped."""
    await session.execute(delete_timer_jobs(run))
    await queue.enqueue(
        session,
        JOB_NODE,
        {"node_run_id": str(run.id), "resume": {"reason": reason, "payload": payload}},
        org_id=run.org_id,
        execution_id=run.execution_id,
        priority=40,
        dedupe_key=f"resume:{run.id}:{reason}",
    )


def delete_timer_jobs(run: NodeRun) -> Any:
    from app.db.models import Job

    return delete(Job).where(Job.dedupe_key.like(f"timer:{run.id}:%"), Job.status == "queued")


async def signal(
    session: AsyncSession,
    org_id: uuid.UUID,
    wait_key: str,
    payload: Any,
    execution_id: uuid.UUID | None = None,
    workspace_ids: set[uuid.UUID] | None = None,
) -> int:
    """Resume nodes waiting on ``wait_key``; ``workspace_ids`` (None = all) limits which workspaces are reached."""
    stmt = (
        select(NodeRun)
        .where(NodeRun.org_id == org_id, NodeRun.wait_key == wait_key, NodeRun.status == S.WAITING.value)
        .with_for_update(skip_locked=True)
    )
    if execution_id is not None:
        stmt = stmt.where(NodeRun.execution_id == execution_id)
    if workspace_ids is not None:
        stmt = stmt.where(
            NodeRun.execution_id.in_(select(Execution.id).where(Execution.workspace_id.in_(workspace_ids)))
        )
    runs = (await session.execute(stmt)).scalars().all()
    for run in runs:
        run.wait_key = None  # a second signal must not resume the node twice
        await resume_node(session, run, "signal", payload)
        session.add(
            ExecutionEvent(
                org_id=run.org_id,
                execution_id=run.execution_id,
                node_id=run.node_id,
                scope=run.scope or None,
                event_type="signal_received",
                message=f"Signal '{wait_key}' received",
            )
        )
    return len(runs)


async def delete_node_runs(session: AsyncSession, execution_id: uuid.UUID) -> None:
    await session.execute(delete(NodeRun).where(NodeRun.execution_id == execution_id))


def group_runs(runs: list[NodeRun]) -> dict[str, dict[str, NodeRun]]:
    out: dict[str, dict[str, NodeRun]] = defaultdict(dict)
    for r in runs:
        out[r.node_id][r.scope] = r
    return out
