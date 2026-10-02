"""Approval inbox and decisions."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import PrincipalDep, SessionDep
from app.core.masking import mask_data
from app.core.rbac import Permission
from app.db.models import ApprovalAction, ApprovalRequest, Execution, Workflow
from app.services import approvals as svc

router = APIRouter(prefix="/approvals", tags=["approvals"])


class DecisionBody(BaseModel):
    comment: str | None = Field(default=None, max_length=5000)
    response_data: dict[str, Any] | None = None


class CommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class ReassignBody(BaseModel):
    user_ids: list[str] = Field(default_factory=list)
    team_ids: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)


def _out(a: ApprovalRequest, principal: Any, workflow_name: str | None = None) -> dict[str, Any]:
    sensitive = principal.has(Permission.EXECUTIONS_READ_SENSITIVE, a.workspace_id)
    return {
        "id": str(a.id),
        "workspace_id": str(a.workspace_id),
        "execution_id": str(a.execution_id),
        "node_id": a.node_id,
        "kind": a.kind,
        "title": a.title,
        "description": a.description,
        "context": a.context if sensitive else mask_data(a.context),
        "form_schema": a.form_schema,
        "status": a.status,
        "approver_user_ids": [str(u) for u in a.approver_user_ids],
        "approver_team_ids": [str(t) for t in a.approver_team_ids],
        "approver_role": a.approver_role,
        "required_approvals": a.required_approvals,
        "approvals_count": a.approvals_count,
        "due_at": a.due_at.isoformat() if a.due_at else None,
        "escalation_level": a.escalation_level,
        "decided_by": str(a.decided_by) if a.decided_by else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decision_comment": a.decision_comment,
        "response_data": a.response_data,
        "created_at": a.created_at.isoformat(),
        "workflow_name": workflow_name,
        "can_decide": svc.can_decide(principal, a) and a.status == "pending",
    }


@router.get("")
async def inbox(
    principal: PrincipalDep,
    session: SessionDep,
    status: str | None = "pending",
    workspace_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    stmt = (
        select(ApprovalRequest, Workflow.name)
        .join(Execution, Execution.id == ApprovalRequest.execution_id)
        .join(Workflow, Workflow.id == Execution.workflow_id)
        .where(svc.approval_visibility_clause(principal))
    )
    if status and status != "all":
        stmt = stmt.where(ApprovalRequest.status == status)
    if workspace_id:
        stmt = stmt.where(ApprovalRequest.workspace_id == workspace_id)
    total = (
        await session.execute(select(func.count()).select_from(stmt.with_only_columns(ApprovalRequest.id).subquery()))
    ).scalar_one()
    order = ApprovalRequest.due_at.asc().nulls_last() if status == "pending" else ApprovalRequest.created_at.desc()
    rows = (await session.execute(stmt.order_by(order).limit(limit).offset(offset))).all()
    return {"items": [_out(a, principal, name) for a, name in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/{approval_id}")
async def get_approval(approval_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    a = await svc.get_approval(session, principal, approval_id)
    ex = await session.get(Execution, a.execution_id)
    wf = await session.get(Workflow, ex.workflow_id) if ex else None
    actions = (
        (
            await session.execute(
                select(ApprovalAction).where(ApprovalAction.approval_id == a.id).order_by(ApprovalAction.created_at)
            )
        )
        .scalars()
        .all()
    )
    out = _out(a, principal, wf.name if wf else None)
    out["history"] = [
        {
            "action": h.action,
            "actor_email": h.actor_email,
            "comment": h.comment,
            "data": h.data,
            "created_at": h.created_at.isoformat(),
        }
        for h in actions
    ]
    return out


@router.post("/{approval_id}/approve")
async def approve(approval_id: uuid.UUID, body: DecisionBody, principal: PrincipalDep, session: SessionDep):
    a = await svc.get_approval(session, principal, approval_id, lock=True)
    await svc.decide(session, principal, a, "approved", body.comment, body.response_data)
    return _out(a, principal)


@router.post("/{approval_id}/reject")
async def reject(approval_id: uuid.UUID, body: DecisionBody, principal: PrincipalDep, session: SessionDep):
    a = await svc.get_approval(session, principal, approval_id, lock=True)
    await svc.decide(session, principal, a, "rejected", body.comment, body.response_data)
    return _out(a, principal)


@router.post("/{approval_id}/comment", status_code=201)
async def comment(approval_id: uuid.UUID, body: CommentBody, principal: PrincipalDep, session: SessionDep):
    a = await svc.get_approval(session, principal, approval_id)
    action = await svc.comment(session, principal, a, body.body)
    return {"action": action.action, "comment": action.comment}


@router.post("/{approval_id}/reassign")
async def reassign(approval_id: uuid.UUID, body: ReassignBody, principal: PrincipalDep, session: SessionDep):
    a = await svc.get_approval(session, principal, approval_id, lock=True)
    await svc.reassign(session, principal, a, body.user_ids, body.team_ids, body.note)
    return _out(a, principal)
