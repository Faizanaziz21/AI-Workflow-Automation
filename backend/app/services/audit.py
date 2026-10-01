"""Audit log writer and search."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.masking import mask_data
from app.core.principal import Principal, request_meta_var
from app.db.models import AUDIT_TSVECTOR_SQL, AuditLog


async def record(
    session: AsyncSession,
    principal: Principal | None,
    action: str,
    *,
    org_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | str | None = None,
    workspace_id: uuid.UUID | None = None,
    summary: str = "",
    details: dict[str, Any] | None = None,
    outcome: str = "success",
    actor_email: str | None = None,
) -> AuditLog:
    meta = request_meta_var.get()
    entry = AuditLog(
        org_id=org_id or (principal.org_id if principal else None),
        workspace_id=workspace_id,
        actor_type=(principal.actor_type.value if principal else "system"),
        actor_id=principal.actor_id if principal else None,
        actor_email=actor_email or (principal.email if principal else None),
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        summary=summary,
        outcome=outcome,
        ip_address=meta.ip_address if meta else None,
        user_agent=meta.user_agent if meta else None,
        request_id=meta.request_id if meta else None,
        details=mask_data(details or {}),
    )
    session.add(entry)
    return entry


async def search(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    query: str | None = None,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> tuple[list[AuditLog], int]:
    stmt = select(AuditLog).where(AuditLog.org_id == org_id)
    if action:
        stmt = stmt.where(
            AuditLog.action.like(action.replace("*", "%")) if "*" in action else AuditLog.action == action
        )
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    if outcome:
        stmt = stmt.where(AuditLog.outcome == outcome)
    if since:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until:
        stmt = stmt.where(AuditLog.created_at <= until)
    if query:
        tsv = literal_column(AUDIT_TSVECTOR_SQL)
        stmt = stmt.where(tsv.op("@@")(func.plainto_tsquery(literal_column("'simple'::regconfig"), query)))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    if before_id:
        stmt = stmt.where(AuditLog.id < before_id)
    rows = (await session.execute(stmt.order_by(AuditLog.id.desc()).limit(limit))).scalars().all()
    return list(rows), int(total)
