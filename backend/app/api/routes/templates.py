"""Template marketplace."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.api.deps import PrincipalDep, SessionDep
from app.core.errors import NotFoundError
from app.core.rbac import Permission
from app.db.models import WorkflowTemplate
from app.services import audit
from app.services import templates as svc

router = APIRouter(prefix="/templates", tags=["templates"])


class InstallBody(BaseModel):
    workspace_id: uuid.UUID
    name: str | None = Field(default=None, max_length=200)
    connections: dict[str, str] = Field(default_factory=dict, description="role -> connection id")


@router.get("")
async def list_templates(
    principal: PrincipalDep, session: SessionDep, category: str | None = None, q: str | None = None
) -> list[dict[str, Any]]:
    stmt = select(WorkflowTemplate)
    if category:
        stmt = stmt.where(WorkflowTemplate.category == category)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(func.lower(WorkflowTemplate.name).like(like), func.lower(WorkflowTemplate.summary).like(like))
        )
    rows = (
        (await session.execute(stmt.order_by(WorkflowTemplate.is_featured.desc(), WorkflowTemplate.name)))
        .scalars()
        .all()
    )
    return [svc.template_out(t) for t in rows]


@router.get("/{slug}")
async def get_template(slug: str, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    t = (await session.execute(select(WorkflowTemplate).where(WorkflowTemplate.slug == slug))).scalar_one_or_none()
    if t is None:
        raise NotFoundError("Template not found")
    return svc.template_out(t, include_definition=True)


@router.post("/{slug}/install", status_code=201)
async def install_template(
    slug: str, body: InstallBody, principal: PrincipalDep, session: SessionDep
) -> dict[str, Any]:
    principal.require(Permission.WORKFLOWS_WRITE, body.workspace_id)
    if not principal.can_access_workspace(body.workspace_id):
        raise NotFoundError("Workspace not found")
    wf = await svc.install(
        session, principal, slug, workspace_id=body.workspace_id, name=body.name, connections=body.connections
    )
    await audit.record(
        session,
        principal,
        "template.installed",
        resource_type="workflow",
        resource_id=wf.id,
        workspace_id=body.workspace_id,
        summary=f"Installed template '{slug}'",
    )
    return {"workflow_id": str(wf.id), "name": wf.name}
