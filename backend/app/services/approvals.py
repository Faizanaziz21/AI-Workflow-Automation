"""Human-in-the-loop approvals: creation, inbox visibility, decisions (with quorum), comments,
reassignment, escalation and expiry. Decisions resume the suspended node in the same transaction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailedError
from app.core.metrics import APPROVALS
from app.core.principal import Principal
from app.db.enums import ApprovalStatus, NodeRunStatus, Role
from app.db.models import ApprovalAction, ApprovalRequest, Execution, NodeRun, Team, User
from app.engine import executor
from app.services import audit


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def _valid_ids(session: AsyncSession, model: Any, org_id: uuid.UUID, ids: list[Any]) -> list[uuid.UUID]:
    parsed = []
    for i in ids or []:
        try:
            parsed.append(uuid.UUID(str(i)))
        except ValueError:
            continue
    if not parsed:
        return []
    rows = (await session.execute(select(model.id).where(model.id.in_(parsed), model.org_id == org_id))).scalars().all()
    return list(rows)


async def create_for_node(session: AsyncSession, ex: Execution, run: NodeRun, spec: dict[str, Any]) -> ApprovalRequest:
    existing = (
        await session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.node_run_id == run.id, ApprovalRequest.status == ApprovalStatus.PENDING.value
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    approval = ApprovalRequest(
        id=uuid.uuid4(),
        org_id=ex.org_id,
        workspace_id=ex.workspace_id,
        execution_id=ex.id,
        node_run_id=run.id,
        node_id=run.node_id,
        kind=spec.get("kind", "approval"),
        title=str(spec.get("title", "Approval required"))[:300],
        description=str(spec.get("description") or ""),
        context=_jsonable(spec.get("context")),
        form_schema=spec.get("form_schema"),
        approver_user_ids=await _valid_ids(session, User, ex.org_id, spec.get("approver_user_ids", [])),
        approver_team_ids=await _valid_ids(session, Team, ex.org_id, spec.get("approver_team_ids", [])),
        approver_role=spec.get("approver_role"),
        required_approvals=int(spec.get("required_approvals", 1)),
        due_at=_parse_dt(spec.get("due_at")),
        escalation=spec.get("escalation") or {},
        response_data=spec.get("response_template"),
    )
    session.add(approval)
    await session.flush()
    session.add(
        ApprovalAction(
            org_id=ex.org_id,
            approval_id=approval.id,
            action="requested",
            comment=approval.title,
            data={"due_at": spec.get("due_at")},
        )
    )
    executor.add_event(
        session,
        ex,
        "approval_requested",
        f"Waiting for {approval.kind.replace('_', ' ')}: {approval.title}",
        node_id=run.node_id,
        scope=run.scope,
        data={"approval_id": str(approval.id)},
    )
    await session.flush()
    return approval


def _jsonable(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    return value if isinstance(value, dict) else {"value": value}


def approval_visibility_clause(principal: Principal) -> Any:
    """SQL predicate: approvals the principal may see/decide."""
    if principal.is_super_admin or Role.ORG_ADMIN in principal.org_roles:
        return ApprovalRequest.org_id == principal.org_id
    clauses = []
    if principal.user_id:
        clauses.append(ApprovalRequest.approver_user_ids.any(principal.user_id))
    if principal.team_ids:
        clauses.append(ApprovalRequest.approver_team_ids.overlap(list(principal.team_ids)))
    org_roles = [r.value for r in principal.org_roles]
    if org_roles:
        clauses.append(ApprovalRequest.approver_role.in_(org_roles))
    for ws_id, roles in principal.workspace_roles.items():
        clauses.append(
            and_(ApprovalRequest.workspace_id == ws_id, ApprovalRequest.approver_role.in_([r.value for r in roles]))
        )
    return and_(ApprovalRequest.org_id == principal.org_id, or_(*clauses) if clauses else false())


def can_decide(principal: Principal, approval: ApprovalRequest) -> bool:
    if principal.is_super_admin or Role.ORG_ADMIN in principal.org_roles:
        return True
    if principal.user_id and principal.user_id in (approval.approver_user_ids or []):
        return True
    if set(approval.approver_team_ids or []) & principal.team_ids:
        return True
    if approval.approver_role and approval.approver_role in {
        r.value for r in principal.roles_for(approval.workspace_id)
    }:
        return True
    return False


async def get_approval(
    session: AsyncSession, principal: Principal, approval_id: uuid.UUID, *, lock: bool = False
) -> ApprovalRequest:
    stmt = select(ApprovalRequest).where(ApprovalRequest.id == approval_id, approval_visibility_clause(principal))
    if lock:
        stmt = stmt.with_for_update()
    approval = (await session.execute(stmt)).scalar_one_or_none()
    if approval is None:
        raise NotFoundError("Approval not found")
    return approval


async def decide(
    session: AsyncSession,
    principal: Principal,
    approval: ApprovalRequest,
    decision: str,
    comment: str | None,
    response_data: dict[str, Any] | None = None,
) -> ApprovalRequest:
    if decision not in ("approved", "rejected"):
        raise ValidationFailedError("decision must be 'approved' or 'rejected'")
    if approval.status != ApprovalStatus.PENDING.value:
        raise ConflictError(f"Approval is already {approval.status}")
    if not can_decide(principal, approval):
        raise PermissionDeniedError("You are not an approver for this request")
    ex = await session.get(Execution, approval.execution_id)
    if ex is None:
        raise NotFoundError("Execution not found")
    is_admin = principal.is_super_admin or Role.ORG_ADMIN in principal.org_roles
    if approval.kind == "approval" and ex.created_by and ex.created_by == principal.user_id and not is_admin:
        raise PermissionDeniedError("Separation of duties: you cannot approve an execution you started")
    already = (
        await session.execute(
            select(func.count())
            .select_from(ApprovalAction)
            .where(
                ApprovalAction.approval_id == approval.id,
                ApprovalAction.actor_id == principal.user_id,
                ApprovalAction.action.in_(["approve", "reject"]),
            )
        )
    ).scalar_one()
    if already and principal.user_id:
        raise ConflictError("You have already recorded a decision on this request")
    if approval.form_schema and decision == "approved":
        _validate_form(approval.form_schema, response_data or {})
    session.add(
        ApprovalAction(
            org_id=approval.org_id,
            approval_id=approval.id,
            actor_id=principal.user_id,
            actor_email=principal.email,
            action="approve" if decision == "approved" else "reject",
            comment=comment,
            data={"response_data": response_data} if response_data else {},
        )
    )
    final = decision == "rejected"
    if decision == "approved":
        approval.approvals_count += 1
        final = approval.approvals_count >= approval.required_approvals
    if response_data is not None:
        approval.response_data = response_data
    APPROVALS.labels(decision).inc()
    await audit.record(
        session,
        principal,
        "approval.decided",
        resource_type="approval",
        resource_id=approval.id,
        workspace_id=approval.workspace_id,
        summary=f"{decision.title()} '{approval.title}'"
        + ("" if final else f" ({approval.approvals_count}/{approval.required_approvals})"),
        details={"execution_id": str(approval.execution_id), "comment": comment},
    )
    if not final:
        executor.add_event(
            session,
            ex,
            "approval_vote",
            f"{principal.email} approved ({approval.approvals_count}/{approval.required_approvals})",
            node_id=approval.node_id,
        )
        return approval
    approval.status = decision
    approval.decided_by = principal.user_id
    approval.decided_at = datetime.now(UTC)
    approval.decision_comment = comment
    run = (
        await session.execute(select(NodeRun).where(NodeRun.id == approval.node_run_id).with_for_update())
    ).scalar_one_or_none()
    if run is not None and run.status in (NodeRunStatus.WAITING_APPROVAL.value, NodeRunStatus.WAITING.value):
        await executor.resume_node(
            session,
            run,
            "approval_decided",
            {
                "decision": decision,
                "comment": comment,
                "decided_by": principal.email or str(principal.actor_id),
                "decided_at": approval.decided_at.isoformat(),
                "response_data": approval.response_data,
                "escalation_level": approval.escalation_level,
            },
        )
    executor.add_event(
        session,
        ex,
        "approval_decided",
        f"{decision.title()} by {principal.email}: {comment or ''}".strip(),
        node_id=approval.node_id,
        data={"approval_id": str(approval.id), "decision": decision},
    )
    return approval


def _validate_form(schema: dict[str, Any], data: dict[str, Any]) -> None:
    import jsonschema

    errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(data)]
    if errors:
        raise ValidationFailedError("Submitted information is invalid", details=errors[:10])


async def comment(session: AsyncSession, principal: Principal, approval: ApprovalRequest, body: str) -> ApprovalAction:
    action = ApprovalAction(
        org_id=approval.org_id,
        approval_id=approval.id,
        actor_id=principal.user_id,
        actor_email=principal.email,
        action="comment",
        comment=body,
    )
    session.add(action)
    await audit.record(
        session,
        principal,
        "approval.commented",
        resource_type="approval",
        resource_id=approval.id,
        workspace_id=approval.workspace_id,
        summary=f"Comment on '{approval.title}'",
    )
    return action


async def reassign(
    session: AsyncSession,
    principal: Principal,
    approval: ApprovalRequest,
    user_ids: list[str],
    team_ids: list[str],
    note: str | None,
) -> ApprovalRequest:
    if approval.status != ApprovalStatus.PENDING.value:
        raise ConflictError(f"Approval is already {approval.status}")
    if not can_decide(principal, approval):
        raise PermissionDeniedError("Only current approvers or admins can reassign")
    approval.approver_user_ids = await _valid_ids(session, User, approval.org_id, user_ids)
    approval.approver_team_ids = await _valid_ids(session, Team, approval.org_id, team_ids)
    session.add(
        ApprovalAction(
            org_id=approval.org_id,
            approval_id=approval.id,
            actor_id=principal.user_id,
            actor_email=principal.email,
            action="reassign",
            comment=note,
            data={
                "user_ids": [str(u) for u in approval.approver_user_ids],
                "team_ids": [str(t) for t in approval.approver_team_ids],
            },
        )
    )
    await audit.record(
        session,
        principal,
        "approval.reassigned",
        resource_type="approval",
        resource_id=approval.id,
        workspace_id=approval.workspace_id,
        summary=f"Reassigned '{approval.title}'",
    )
    return approval


async def escalate(session: AsyncSession, approval: ApprovalRequest, spec: dict[str, Any]) -> ApprovalRequest:
    approval.escalation_level += 1
    approval.escalated_at = datetime.now(UTC)
    approval.approver_user_ids = list(
        dict.fromkeys(
            [
                *approval.approver_user_ids,
                *await _valid_ids(session, User, approval.org_id, spec.get("approver_user_ids", [])),
            ]
        )
    )
    approval.approver_team_ids = list(
        dict.fromkeys(
            [
                *approval.approver_team_ids,
                *await _valid_ids(session, Team, approval.org_id, spec.get("approver_team_ids", [])),
            ]
        )
    )
    if spec.get("approver_role"):
        approval.approver_role = spec["approver_role"]
    approval.due_at = _parse_dt(spec.get("due_at")) or approval.due_at
    session.add(
        ApprovalAction(
            org_id=approval.org_id,
            approval_id=approval.id,
            action="escalate",
            comment=f"Escalated to level {approval.escalation_level}",
            data={"due_at": spec.get("due_at")},
        )
    )
    await audit.record(
        session,
        None,
        "approval.escalated",
        org_id=approval.org_id,
        resource_type="approval",
        resource_id=approval.id,
        workspace_id=approval.workspace_id,
        summary=f"'{approval.title}' escalated (level {approval.escalation_level})",
    )
    return approval


async def expire(session: AsyncSession, approval: ApprovalRequest, outcome: str) -> None:
    if approval.status != ApprovalStatus.PENDING.value:
        return
    approval.status = ApprovalStatus.EXPIRED.value
    approval.decided_at = datetime.now(UTC)
    approval.decision_comment = f"Deadline passed; outcome: {outcome}"
    session.add(
        ApprovalAction(
            org_id=approval.org_id, approval_id=approval.id, action="expire", comment=approval.decision_comment
        )
    )
    await audit.record(
        session,
        None,
        "approval.expired",
        org_id=approval.org_id,
        resource_type="approval",
        resource_id=approval.id,
        workspace_id=approval.workspace_id,
        summary=f"'{approval.title}' expired ({outcome})",
    )


def as_dict(approval: ApprovalRequest) -> dict[str, Any]:
    return {
        "id": str(approval.id),
        "status": approval.status,
        "kind": approval.kind,
        "title": approval.title,
        "escalation_level": approval.escalation_level,
        "approvals_count": approval.approvals_count,
        "required_approvals": approval.required_approvals,
        "due_at": approval.due_at.isoformat() if approval.due_at else None,
        "decision": approval.status if approval.status in ("approved", "rejected") else None,
        "comment": approval.decision_comment,
        "decided_by": str(approval.decided_by) if approval.decided_by else None,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "response_data": approval.response_data,
    }
