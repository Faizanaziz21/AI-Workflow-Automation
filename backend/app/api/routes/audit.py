from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import SessionDep, require
from app.core.principal import Principal
from app.core.rbac import Permission
from app.schemas.common import Page
from app.schemas.tenancy import AuditLogOut
from app.services import audit

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("", response_model=Page[AuditLogOut])
async def search_audit_logs(
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.AUDIT_READ))],
    q: Annotated[str | None, Query(max_length=200)] = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    actor_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    before_id: int | None = None,
):
    rows, total = await audit.search(
        session,
        principal.org_id,
        query=q,
        action=action,
        actor_id=actor_id,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        since=since,
        until=until,
        limit=limit,
        before_id=before_id,
    )
    next_cursor = str(rows[-1].id) if len(rows) == limit else None
    return Page(items=[AuditLogOut.model_validate(r) for r in rows], total=total, limit=limit, next_cursor=next_cursor)
