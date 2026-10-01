"""Node execution.

Protocol for one ``node.run`` job (two short transactions around the handler):

1. **Claim** — lock the node run, verify it is runnable (scheduled/retrying/waiting-with-resume, or RUNNING
   left behind by a crashed worker), mark it RUNNING, bump the attempt counter. Commit.
2. **Execute** — build the data context from checkpoints, render the config, validate it, and run the handler
   under the node timeout. No transaction is held while user code / remote calls run.
3. **Persist** — lock the node run again; if it was cancelled meanwhile the result is discarded. Otherwise store
   output (secret-masked, size-capped) or error, schedule a retry with backoff / a timer for waits, and enqueue
   ``execution.advance`` — all in one commit.

Handlers receive an idempotency key that is stable across retries and crash re-deliveries, which connectors
forward to remote APIs (``Idempotency-Key``, deterministic Message-ID, ...).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from app.ai.gateway import AIGateway
from app.connectors.sdk import ConnectorError, FileStore
from app.core.config import get_settings
from app.core.logging import execution_id_var
from app.core.masking import mask_data
from app.core.metrics import NODE_DURATION, NODE_RETRIES, NODE_RUNS
from app.core.telemetry import get_tracer
from app.db.enums import NodeRunStatus, TriggerType
from app.db.models import ApprovalRequest, Execution, NodeRun, Workflow
from app.db.session import get_sessionmaker
from app.engine import executor, queue
from app.engine.definition import NodeDef, WorkflowDefinition
from app.engine.expressions import ExpressionError, render
from app.engine.graph import LOOP_BODY_HANDLE, LOOP_TYPE, Graph, scope_chain
from app.nodes.base import Completed, NodeContext, NodeError, Wait
from app.nodes.registry import get_node_type
from app.services import approvals as approval_service
from app.services import connections as connection_service
from app.services.storage import PlatformFileStore

logger = logging.getLogger(__name__)
tracer = get_tracer("flowforge.engine")
S = NodeRunStatus


class EngineRuntime:
    """Concrete :class:`app.nodes.base.NodeRuntime` for one node run."""

    def __init__(self, ex: Execution, run: NodeRun, node: NodeDef) -> None:
        self.ex = ex
        self.run = run
        self.node = node
        self.sessionmaker = get_sessionmaker()
        self.secret_values: list[str] = []
        self._files = PlatformFileStore(self.sessionmaker, ex.org_id, ex.workspace_id)
        self._gateway: AIGateway | None = None

    def register_secrets(self, values: list[str]) -> None:
        self.secret_values.extend(v for v in values if v)

    async def connection(self, connection_id: str) -> connection_service.ResolvedConnection:
        async with self.sessionmaker() as session:
            resolved = await connection_service.resolve(
                session,
                self.ex.org_id,
                connection_id,
                workspace_id=self.ex.workspace_id,
                idempotency_key=self.run.idempotency_key,
            )
        self.register_secrets(resolved.secret_values)
        return resolved

    def ai(self) -> AIGateway:
        if self._gateway is None:
            self._gateway = AIGateway(
                self.sessionmaker,
                org_id=self.ex.org_id,
                workspace_id=self.ex.workspace_id,
                resolve_connection=self.connection,
                execution_id=self.ex.id,
                node_id=self.node.id,
            )
        return self._gateway

    @property
    def files(self) -> FileStore:
        return self._files

    async def create_approval(self, spec: dict[str, Any]) -> str:
        async with self.sessionmaker() as session:
            ex = await session.get(Execution, self.ex.id)
            run = await session.get(NodeRun, self.run.id)
            assert ex is not None and run is not None
            approval = await approval_service.create_for_node(session, ex, run, spec)
            await session.commit()
            return str(approval.id)

    async def _approval(self, session: Any, approval_id: str) -> ApprovalRequest:
        approval = (
            await session.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.id == uuid.UUID(approval_id), ApprovalRequest.org_id == self.ex.org_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if approval is None:
            raise NodeError(f"Approval {approval_id} not found")
        return approval

    async def get_approval(self, approval_id: str) -> dict[str, Any]:
        async with self.sessionmaker() as session:
            return approval_service.as_dict(await self._approval(session, approval_id))

    async def escalate_approval(self, approval_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        async with self.sessionmaker() as session:
            approval = await approval_service.escalate(session, await self._approval(session, approval_id), spec)
            result = approval_service.as_dict(approval)
            await session.commit()
            return result

    async def expire_approval(self, approval_id: str, outcome: str) -> None:
        async with self.sessionmaker() as session:
            await approval_service.expire(session, await self._approval(session, approval_id), outcome)
            await session.commit()

    async def set_webhook_response(self, response: dict[str, Any]) -> None:
        await executor.push_webhook_response(self.ex.id, response)

    async def start_child_execution(self, workflow_id: str, payload: dict[str, Any]) -> str:
        try:
            wid = uuid.UUID(str(workflow_id))
        except ValueError as exc:
            raise NodeError(f"Invalid workflow id {workflow_id!r}") from exc
        async with self.sessionmaker() as session:
            wf = (
                await session.execute(
                    select(Workflow).where(
                        Workflow.id == wid,
                        Workflow.org_id == self.ex.org_id,
                        Workflow.workspace_id == self.ex.workspace_id,
                    )
                )
            ).scalar_one_or_none()
            if wf is None or wf.published_version_id is None:
                raise NodeError("Sub-workflow not found or not published in this workspace")
            if wf.id == self.ex.workflow_id:
                raise NodeError("A workflow cannot call itself")
            child, _ = await executor.start_execution(
                session,
                org_id=self.ex.org_id,
                workspace_id=self.ex.workspace_id,
                workflow_id=wf.id,
                version_id=wf.published_version_id,
                trigger_type=TriggerType.SUB_WORKFLOW.value,
                payload=payload,
                idempotency_key=f"child:{self.run.idempotency_key}",
                parent_execution_id=self.ex.id,
                created_by=self.ex.created_by,
            )
            await session.commit()
            return str(child.id)


# ----------------------------------------------------------------------------- context


def build_data(
    ex: Execution, definition: WorkflowDefinition, graph: Graph, runs: list[NodeRun], node_id: str, scope: str
) -> dict[str, Any]:
    chain = scope_chain(scope)
    grouped = executor.group_runs(runs)
    nodes: dict[str, Any] = {}
    for nid, by_scope in grouped.items():
        for s in chain:
            r = by_scope.get(s)
            if r is not None:
                nodes[nid] = {"output": r.output, "status": r.status, "error": r.error}
                break
    variables = dict(definition.variables)
    visible_sets = []
    for by_scope in grouped.values():
        for s in chain:
            r = by_scope.get(s)
            if r is not None:
                if r.node_type == "logic.set_variable" and r.status == S.COMPLETED.value and isinstance(r.output, dict):
                    visible_sets.append(r)
                break
    for r in sorted(visible_sets, key=lambda r: r.finished_at or datetime.min.replace(tzinfo=UTC)):
        variables.update(r.output)
    trigger_id = graph.trigger_ids()[0]
    trigger_run = grouped.get(trigger_id, {}).get("")
    loops: dict[str, Any] = {}
    loop: dict[str, Any] | None = None
    if scope:
        for part_scope in reversed(chain[:-1]):  # outermost first
            last = part_scope.rsplit("/", 1)[-1]
            loop_id, idx = last.split(":")
            parent = part_scope.rsplit("/", 1)[0] if "/" in part_scope else ""
            loop_run = grouped.get(loop_id, {}).get(parent)
            items = (loop_run.state or {}).get("loop_items", []) if loop_run else []
            i = int(idx)
            loops[loop_id] = {
                "item": items[i] if i < len(items) else None,
                "index": i,
                "count": len(items),
                "is_last": i == len(items) - 1,
            }
            loop = {**loops[loop_id], "loop_id": loop_id}
    incoming = []
    for e in graph.incoming[node_id]:
        if graph.parent_loop.get(node_id) == e.source and e.source_handle == LOOP_BODY_HANDLE:
            continue
        src = grouped.get(e.source, {}).get(scope)
        if src is not None and src.status == S.COMPLETED.value and e.source_handle in (src.branches or ["out"]):
            incoming.append(e.source)
    return {
        "trigger": trigger_run.output if trigger_run else ex.trigger_payload,
        "nodes": nodes,
        "vars": variables,
        "loop": loop,
        "loops": loops,
        "incoming": incoming,
        "execution": {
            "id": str(ex.id),
            "workflow_id": str(ex.workflow_id),
            "trigger_type": ex.trigger_type,
            "started_at": ex.started_at.isoformat() if ex.started_at else None,
            "retry_count": ex.retry_count,
            "correlation_key": ex.correlation_key,
        },
    }


def render_config(node_type: Any, config: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for key, value in config.items():
        rendered[key] = value if key in node_type.raw_fields else render(value, data)
    return rendered


def _timeout_for(definition: WorkflowDefinition, node: NodeDef, node_type: Any) -> float:
    return float(
        node.timeout_seconds
        or definition.settings.default_node_timeout_seconds
        or node_type.default_timeout_seconds
        or get_settings().default_node_timeout_seconds
    )


def _error_dict(exc: BaseException, attempt: int, secret_values: list[str]) -> dict[str, Any]:
    if isinstance(exc, NodeError):
        err = {
            "type": "NodeError",
            "message": exc.message,
            "retryable": exc.retryable,
            "code": exc.code,
            "details": exc.details,
        }
    elif isinstance(exc, ConnectorError):
        err = {
            "type": "ConnectorError",
            "message": exc.message,
            "retryable": exc.retryable,
            "code": f"http_{exc.status_code}" if exc.status_code else None,
            "details": exc.details,
        }
    elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        err = {"type": "Timeout", "message": "Node exceeded its timeout", "retryable": True, "code": "timeout"}
    elif isinstance(exc, ExpressionError):
        err = {
            "type": "ExpressionError",
            "message": exc.message,
            "retryable": False,
            "code": "expression_error",
            "details": {"expression": exc.expression},
        }
    elif isinstance(exc, ValidationError):
        err = {
            "type": "ConfigError",
            "message": "Node configuration is invalid after rendering expressions",
            "retryable": False,
            "code": "invalid_config",
            "details": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()[:20]],
        }
    else:
        err = {
            "type": type(exc).__name__,
            "message": f"Unexpected error: {exc}",
            "retryable": False,
            "code": "internal_error",
        }
    err["attempt"] = attempt
    result: dict[str, Any] = mask_data({k: v for k, v in err.items() if v is not None}, secret_values)
    return result


def _cap_output(output: Any, limit: int) -> Any:
    size = len(json.dumps(output, default=str))
    if size > limit:
        raise NodeError(
            f"Node output is {size} bytes, above the {limit} byte limit; store large data as a file",
            code="output_too_large",
        )
    return json.loads(json.dumps(output, default=str))


# ----------------------------------------------------------------------------- job handler


async def run_node(payload: dict[str, Any], worker_id: str) -> None:
    run_id = uuid.UUID(payload["node_run_id"])
    resume: dict[str, Any] | None = payload.get("resume")
    timer_seq: int | None = payload.get("wait_seq")
    sm = get_sessionmaker()

    # --- 1. claim
    async with sm() as session:
        run = (
            await session.execute(select(NodeRun).where(NodeRun.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None:
            return
        ex = await session.get(Execution, run.execution_id)
        if ex is None:
            return
        if ex.cancel_requested or ex.status in ("COMPLETED", "FAILED", "CANCELLED"):
            if run.status not in (S.COMPLETED.value, S.FAILED.value, S.SKIPPED.value, S.CANCELLED.value):
                run.status = S.CANCELLED.value
                run.finished_at = datetime.now(UTC)
                await session.commit()
            return
        state = dict(run.state or {})
        if resume is not None:
            waiting = run.status in (S.WAITING.value, S.WAITING_APPROVAL.value)
            redelivered = run.status == S.RUNNING.value and state.get("resume") == resume
            if not (waiting or redelivered):
                return  # stale resume (already resumed / finished)
            if (
                resume.get("reason") == "timer"
                and waiting
                and timer_seq is not None
                and state.get("wait_seq") != timer_seq
            ):
                return  # timer from an earlier wait
            state["resume"] = resume
            run.state = state
        elif run.status not in (S.SCHEDULED.value, S.RETRYING.value, S.RUNNING.value):
            return  # duplicate delivery of an already-finished node
        else:
            if run.status == S.RUNNING.value:
                executor.add_event(
                    session,
                    ex,
                    "node_recovered",
                    "Re-running node after worker interruption",
                    node_id=run.node_id,
                    scope=run.scope,
                    level="warning",
                )
            run.attempt += 1
        run.status = S.RUNNING.value
        run.worker_id = worker_id
        run.started_at = run.started_at or datetime.now(UTC)
        if resume is None:
            executor.add_event(
                session,
                ex,
                "node_started",
                f"Attempt {run.attempt}",
                node_id=run.node_id,
                scope=run.scope,
                data={"attempt": run.attempt},
            )
        else:
            executor.add_event(
                session, ex, "node_resumed", f"Resumed ({resume.get('reason')})", node_id=run.node_id, scope=run.scope
            )
        await session.commit()
        attempt = run.attempt

    # --- 2. execute
    with tracer.start_as_current_span(
        f"node {run.node_type}",
        attributes={
            "flowforge.execution_id": str(ex.id),
            "flowforge.node_id": run.node_id,
            "flowforge.attempt": attempt,
        },
    ):
        token = execution_id_var.set(str(ex.id))
        try:
            await _execute_and_persist(sm, ex, run, resume, attempt, worker_id)
        finally:
            execution_id_var.reset(token)


async def _execute_and_persist(
    sm: Any, ex: Execution, run: NodeRun, resume: dict[str, Any] | None, attempt: int, worker_id: str
) -> None:
    async with sm() as session:
        definition, graph = await executor.load_definition(session, ex.workflow_version_id)
        runs = list((await session.execute(select(NodeRun).where(NodeRun.execution_id == ex.id))).scalars().all())
    node = definition.node(run.node_id)
    node_type = get_node_type(node.type)
    runtime = EngineRuntime(ex, run, node)
    timeout = _timeout_for(definition, node, node_type)
    data = build_data(ex, definition, graph, runs, run.node_id, run.scope)
    ctx = NodeContext(
        org_id=ex.org_id,
        workspace_id=ex.workspace_id,
        workflow_id=ex.workflow_id,
        execution_id=ex.id,
        node=node,
        scope=run.scope,
        attempt=attempt,
        idempotency_key=run.idempotency_key,
        data=data,
        runtime=runtime,
        resume=resume,
        state={k: v for k, v in (run.state or {}).items() if k != "resume"},
        timeout=timeout,
    )
    started = time.perf_counter()
    result: Completed | Wait | None = None
    error: BaseException | None = None
    rendered: dict[str, Any] | None = None
    try:
        rendered = render_config(node_type, node.config, data)
        config = node_type.config_model.model_validate(rendered)
        result = await asyncio.wait_for(node_type.execute(ctx, config), timeout=timeout)
        if isinstance(result, Completed):
            result.output = _cap_output(result.output, get_settings().max_node_output_bytes)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        error = exc
        if not isinstance(exc, (NodeError, ConnectorError, ExpressionError, ValidationError, asyncio.TimeoutError)):
            logger.exception("node %s raised unexpectedly", run.node_id)
    elapsed = time.perf_counter() - started
    NODE_DURATION.labels(node.type).observe(elapsed)
    secrets_ = runtime.secret_values

    # --- 3. persist
    async with sm() as session:
        current = (
            await session.execute(select(NodeRun).where(NodeRun.id == run.id).with_for_update())
        ).scalar_one_or_none()
        ex_now = await session.get(Execution, ex.id)
        if current is None or ex_now is None or current.status != S.RUNNING.value or current.worker_id != worker_id:
            return  # cancelled / replayed while running: discard
        now = datetime.now(UTC)
        state = {k: v for k, v in (current.state or {}).items() if k != "resume"}
        if rendered is not None and node.type != LOOP_TYPE:
            current.input = mask_data({"config": rendered}, secrets_)
        for entry in ctx.logs:
            executor.add_event(
                session,
                ex_now,
                "log",
                entry["message"],
                node_id=run.node_id,
                scope=run.scope,
                level=entry["level"],
                data=mask_data(entry["data"], secrets_),
            )
        policy = executor.retry_policy_for(definition, run.node_id)
        if isinstance(result, Completed):
            current.status = S.COMPLETED.value
            current.output = mask_data(result.output, secrets_, mask_keys=False)
            current.branches = result.branches if result.branches is not None else ["out"]
            current.error = None
            current.wait_key = None
            current.finished_at = now
            current.duration_ms = int(
                ((now - current.started_at).total_seconds() if current.started_at else elapsed) * 1000
            )
            current.state = state
            NODE_RUNS.labels(node.type, "completed").inc()
            executor.add_event(
                session,
                ex_now,
                "node_completed",
                f"Completed in {int(elapsed * 1000)} ms",
                node_id=run.node_id,
                scope=run.scope,
                data={"duration_ms": int(elapsed * 1000), "branches": current.branches},
            )
        elif isinstance(result, Wait):
            seq = int(state.get("wait_seq", 0)) + 1
            current.state = {**state, **result.state, "wait_seq": seq}
            current.status = result.status
            current.wait_key = result.wait_key
            current.wait_until = result.until
            if node.type == LOOP_TYPE:
                current.status = S.WAITING.value
                current.input = {"config": {"items_count": len(result.state.get("loop_items", []))}}
            elif result.until is not None:
                await queue.enqueue(
                    session,
                    executor.JOB_NODE,
                    {"node_run_id": str(run.id), "resume": {"reason": "timer"}, "wait_seq": seq},
                    org_id=ex.org_id,
                    execution_id=ex.id,
                    available_at=result.until,
                    dedupe_key=f"timer:{run.id}:{seq}",
                    priority=60,
                )
            NODE_RUNS.labels(node.type, "waiting").inc()
            if node.type != LOOP_TYPE:
                executor.add_event(
                    session,
                    ex_now,
                    "node_waiting",
                    result.reason or "Waiting",
                    node_id=run.node_id,
                    scope=run.scope,
                    data={"until": result.until.isoformat() if result.until else None, "wait_key": result.wait_key},
                )
        else:
            assert error is not None
            err = _error_dict(error, attempt, secrets_)
            retryable = bool(err.get("retryable")) or policy.retry_on == "all"
            current.error = err
            current.state = state
            if retryable and attempt < current.max_attempts and not ex_now.cancel_requested:
                delay = policy.delay_for(attempt, random.random())  # noqa: S311 - jitter
                current.status = S.RETRYING.value
                await queue.enqueue(
                    session,
                    executor.JOB_NODE,
                    {"node_run_id": str(run.id)},
                    org_id=ex.org_id,
                    execution_id=ex.id,
                    available_at=now + timedelta(seconds=delay),
                    dedupe_key=f"retry:{run.id}:{attempt}",
                )
                NODE_RETRIES.labels(node.type).inc()
                executor.add_event(
                    session,
                    ex_now,
                    "node_retry_scheduled",
                    f"Attempt {attempt} failed: {err['message']} — retrying in {delay:.1f}s",
                    node_id=run.node_id,
                    scope=run.scope,
                    level="warning",
                    data={"attempt": attempt, "delay_seconds": round(delay, 2)},
                )
            else:
                current.finished_at = now
                current.duration_ms = int(
                    ((now - current.started_at).total_seconds() if current.started_at else elapsed) * 1000
                )
                if node.on_error in ("continue", "route"):
                    current.status = S.COMPLETED.value
                    current.output = {"error": err}
                    current.branches = ["error"] if node.on_error == "route" else ["out"]
                    executor.add_event(
                        session,
                        ex_now,
                        "node_error_handled",
                        f"{err['message']} (on_error={node.on_error})",
                        node_id=run.node_id,
                        scope=run.scope,
                        level="warning",
                    )
                else:
                    current.status = S.FAILED.value
                    executor.add_event(
                        session,
                        ex_now,
                        "node_failed",
                        err["message"],
                        node_id=run.node_id,
                        scope=run.scope,
                        level="error",
                        data={"attempt": attempt},
                    )
                NODE_RUNS.labels(node.type, "failed").inc()
        await executor.enqueue_advance(session, ex_now)
        await session.commit()


__all__ = ["EngineRuntime", "build_data", "render_config", "run_node"]
