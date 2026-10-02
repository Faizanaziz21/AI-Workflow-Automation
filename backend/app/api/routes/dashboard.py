"""Monitoring dashboard API."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

from app.api.deps import PrincipalDep, SessionDep
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.principal import Principal
from app.core.rbac import Permission
from app.services import dashboard as svc

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
Window = Literal["15m", "1h", "6h", "24h", "7d", "30d"]


def _workspaces(principal: Principal, workspace_id: uuid.UUID | None) -> set[uuid.UUID] | None:
    allowed = principal.workspaces_with(Permission.DASHBOARD_READ)
    if allowed is not None and not allowed:
        raise PermissionDeniedError("Missing permission 'dashboard:read'")
    if workspace_id is not None and allowed is not None and workspace_id not in allowed:
        raise NotFoundError("Workspace not found")
    return allowed


@router.get("/summary")
async def summary(
    principal: PrincipalDep, session: SessionDep, window: Window = "24h", workspace_id: uuid.UUID | None = None
) -> dict[str, Any]:
    return await svc.summary(session, principal.org_id, _workspaces(principal, workspace_id), workspace_id, window)


@router.get("/timeseries")
async def timeseries(
    principal: PrincipalDep, session: SessionDep, window: Window = "24h", workspace_id: uuid.UUID | None = None
) -> dict[str, Any]:
    return await svc.timeseries(session, principal.org_id, _workspaces(principal, workspace_id), workspace_id, window)


@router.get("/workflows")
async def workflows(
    principal: PrincipalDep,
    session: SessionDep,
    window: Window = "24h",
    workspace_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, Any]]:
    return await svc.workflow_stats(
        session, principal.org_id, _workspaces(principal, workspace_id), workspace_id, window, limit
    )


@router.get("/nodes")
async def nodes(
    principal: PrincipalDep,
    session: SessionDep,
    window: Window = "24h",
    workspace_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, Any]]:
    return await svc.node_stats(
        session, principal.org_id, _workspaces(principal, workspace_id), workspace_id, window, limit
    )
