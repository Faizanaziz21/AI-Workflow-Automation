"""Workflow CRUD, drafts, validation, publishing, versions, diff, rollback, clone, archive, triggers."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.api.deps import PrincipalDep, SessionDep, WorkspaceDep
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.rbac import Permission
from app.db.models import Execution, Secret, Workflow, WorkflowTrigger, WorkflowVersion
from app.engine.validation import validate_definition
from app.nodes.registry import all_node_types, get_node_type
from app.schemas.common import ORMModel, Page
from app.services import audit
from app.services import workflows as svc
from app.services.connections import read_secret, write_secret

router = APIRouter(tags=["workflows"])


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    definition: dict[str, Any] | None = None


class WorkflowPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = Field(default=None, max_length=20)


class DraftSave(BaseModel):
    definition: dict[str, Any]
    expected_revision: int | None = None


class PublishBody(BaseModel):
    change_note: str = Field(default="", max_length=2000)


class RollbackBody(BaseModel):
    version: int = Field(ge=1)
    change_note: str = Field(default="", max_length=2000)


class CloneBody(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    workspace_id: uuid.UUID | None = None


class VersionSummary(ORMModel):
    id: uuid.UUID
    version: int
    status: str
    revision: int
    definition_hash: str
    change_note: str
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    published_by: uuid.UUID | None


class VersionOut(VersionSummary):
    definition: dict[str, Any]


class WorkflowOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    description: str
    tags: list[str]
    status: str
    published_version_id: uuid.UUID | None
    latest_version: int
    template_slug: str | None
    created_at: datetime
    updated_at: datetime
    trigger_type: str | None = None
    published_version: int | None = None
    has_draft: bool = False
    executions_24h: int | None = None


class WorkflowDetail(WorkflowOut):
    draft: VersionOut | None = None
    published: VersionOut | None = None


async def _summary(session, wf: Workflow) -> dict[str, Any]:
    trig = (
        await session.execute(select(WorkflowTrigger.trigger_type).where(WorkflowTrigger.workflow_id == wf.id))
    ).scalar_one_or_none()
    pub = await svc.get_published(session, wf)
    draft = await svc.get_draft(session, wf)
    trigger_type = trig
    if trigger_type is None:
        source = draft or pub
        if source:
            node = next((n for n in source.definition["nodes"] if n["type"].startswith("trigger.")), None)
            trigger_type = node["type"].removeprefix("trigger.") if node else None
    return {
        "trigger_type": trigger_type,
        "published_version": pub.version if pub else None,
        "has_draft": draft is not None,
        "_draft": draft,
        "_pub": pub,
    }


# ----------------------------------------------------------------------------- catalog


@router.get("/node-types")
async def list_node_types(principal: PrincipalDep, category: str | None = None) -> list[dict[str, Any]]:
    return [n.describe() for n in all_node_types() if category is None or n.category == category]


class HandlesQuery(BaseModel):
    nodes: list[dict[str, Any]] = Field(max_length=500)


@router.post("/node-types/handles")
async def node_handles(body: HandlesQuery, principal: PrincipalDep) -> dict[str, list[str]]:
    """Output handles for each node given its current config (dynamic handles such as Switch cases)."""
    out: dict[str, list[str]] = {}
    for n in body.nodes:
        try:
            nt = get_node_type(str(n.get("type")))
        except KeyError:
            continue
        handles = nt.handles(n.get("config") or {})
        if n.get("on_error") == "route":
            handles = [*handles, "error"]
        out[str(n.get("id"))] = handles
    return out


@router.get("/node-types/{type_key}")
async def get_node_type_detail(type_key: str, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return get_node_type(type_key).describe()
    except KeyError as exc:
        raise NotFoundError("Node type not found") from exc


# ----------------------------------------------------------------------------- workflows


@router.get("/workspaces/{workspace_id}/workflows", response_model=Page[WorkflowOut])
async def list_workflows(
    ws: WorkspaceDep,
    principal: PrincipalDep,
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: str | None = "active",
    tag: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    principal.require(Permission.WORKFLOWS_READ, ws.id)
    stmt = select(Workflow).where(Workflow.org_id == principal.org_id, Workflow.workspace_id == ws.id)
    if status and status != "all":
        stmt = stmt.where(Workflow.status == status)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Workflow.name).like(like), func.lower(Workflow.description).like(like)))
    if tag:
        stmt = stmt.where(Workflow.tags.any(tag))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (await session.execute(stmt.order_by(Workflow.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    )
    ids = [r.id for r in rows]
    counts: dict[uuid.UUID, int] = {}
    if ids:
        since = func.now() - func.make_interval(0, 0, 0, 1)
        counts = dict(
            (
                await session.execute(
                    select(Execution.workflow_id, func.count())
                    .where(Execution.workflow_id.in_(ids), Execution.created_at >= since)
                    .group_by(Execution.workflow_id)
                )
            ).all()
        )
    items = []
    for wf in rows:
        meta = await _summary(session, wf)
        out = WorkflowOut.model_validate(wf)
        out.trigger_type, out.published_version, out.has_draft = (
            meta["trigger_type"],
            meta["published_version"],
            meta["has_draft"],
        )
        out.executions_24h = counts.get(wf.id, 0)
        items.append(out)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("/workspaces/{workspace_id}/workflows", response_model=WorkflowDetail, status_code=201)
async def create_workflow(body: WorkflowCreate, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    principal.require(Permission.WORKFLOWS_WRITE, ws.id)
    wf, _ = await svc.create_workflow(
        session,
        principal,
        workspace_id=ws.id,
        name=body.name,
        description=body.description,
        tags=body.tags,
        definition=body.definition,
    )
    return await _detail(session, wf)


async def _detail(session, wf: Workflow) -> WorkflowDetail:
    meta = await _summary(session, wf)
    out = WorkflowDetail.model_validate(wf)
    out.trigger_type, out.published_version, out.has_draft = (
        meta["trigger_type"],
        meta["published_version"],
        meta["has_draft"],
    )
    out.draft = VersionOut.model_validate(meta["_draft"]) if meta["_draft"] else None
    out.published = VersionOut.model_validate(meta["_pub"]) if meta["_pub"] else None
    return out


@router.get("/workspaces/{workspace_id}/workflows/{workflow_id}", response_model=WorkflowDetail)
async def get_workflow(workflow_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    principal.require(Permission.WORKFLOWS_READ, ws.id)
    return await _detail(session, await svc.get_workflow(session, principal, workflow_id, ws.id))


@router.patch("/workspaces/{workspace_id}/workflows/{workflow_id}", response_model=WorkflowDetail)
async def patch_workflow(
    workflow_id: uuid.UUID, body: WorkflowPatch, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep
):
    principal.require(Permission.WORKFLOWS_WRITE, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(wf, k, v)
    await audit.record(
        session,
        principal,
        "workflow.modified",
        resource_type="workflow",
        resource_id=wf.id,
        workspace_id=ws.id,
        summary=f"Workflow '{wf.name}' details updated",
        details={"fields": sorted(changes)},
    )
    return await _detail(session, wf)


@router.put("/workspaces/{workspace_id}/workflows/{workflow_id}/draft", response_model=VersionOut)
async def save_draft(
    workflow_id: uuid.UUID, body: DraftSave, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep
):
    principal.require(Permission.WORKFLOWS_WRITE, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id, lock=True)
    return await svc.save_draft(session, principal, wf, body.definition, body.expected_revision)


@router.post("/workspaces/{workspace_id}/workflows/{workflow_id}/validate")
async def validate_workflow(
    workflow_id: uuid.UUID,
    ws: WorkspaceDep,
    principal: PrincipalDep,
    session: SessionDep,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    principal.require(Permission.WORKFLOWS_READ, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    definition = (body or {}).get("definition")
    if definition is None:
        source = await svc.get_draft(session, wf) or await svc.get_published(session, wf)
        definition = source.definition if source else svc.EMPTY_DEFINITION
    return validate_definition(definition).as_dict()


@router.post("/workspaces/{workspace_id}/workflows/{workflow_id}/publish", response_model=VersionOut)
async def publish_workflow(
    workflow_id: uuid.UUID, body: PublishBody, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep
):
    principal.require(Permission.WORKFLOWS_PUBLISH, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id, lock=True)
    return await svc.publish(session, principal, wf, body.change_note)


@router.get("/workspaces/{workspace_id}/workflows/{workflow_id}/versions", response_model=list[VersionSummary])
async def list_versions(workflow_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    principal.require(Permission.WORKFLOWS_READ, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    return (
        (
            await session.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == wf.id)
                .order_by(WorkflowVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )


@router.get("/workspaces/{workspace_id}/workflows/{workflow_id}/versions/diff")
async def diff_versions(
    workflow_id: uuid.UUID,
    ws: WorkspaceDep,
    principal: PrincipalDep,
    session: SessionDep,
    from_version: Annotated[int, Query(alias="from", ge=1)],
    to_version: Annotated[int, Query(alias="to", ge=1)],
) -> dict[str, Any]:
    principal.require(Permission.WORKFLOWS_READ, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    a = await svc.get_version(session, wf, from_version)
    b = await svc.get_version(session, wf, to_version)
    return {"from": from_version, "to": to_version, **svc.diff_definitions(a.definition, b.definition)}


@router.get("/workspaces/{workspace_id}/workflows/{workflow_id}/versions/{version}", response_model=VersionOut)
async def get_version(
    workflow_id: uuid.UUID, version: int, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep
):
    principal.require(Permission.WORKFLOWS_READ, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    return await svc.get_version(session, wf, version)


@router.post("/workspaces/{workspace_id}/workflows/{workflow_id}/rollback", response_model=VersionOut)
async def rollback(
    workflow_id: uuid.UUID, body: RollbackBody, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep
):
    principal.require(Permission.WORKFLOWS_PUBLISH, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id, lock=True)
    return await svc.rollback(session, principal, wf, body.version, body.change_note)


@router.post("/workspaces/{workspace_id}/workflows/{workflow_id}/clone", response_model=WorkflowDetail, status_code=201)
async def clone(
    workflow_id: uuid.UUID, body: CloneBody, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep
):
    principal.require(Permission.WORKFLOWS_WRITE, ws.id)
    target_ws = body.workspace_id or ws.id
    principal.require(Permission.WORKFLOWS_WRITE, target_ws)
    if not principal.can_access_workspace(target_ws):
        raise NotFoundError("Workspace not found")
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    new_wf = await svc.clone(session, principal, wf, body.name, target_ws)
    return await _detail(session, new_wf)


@router.post("/workspaces/{workspace_id}/workflows/{workflow_id}/archive", response_model=WorkflowDetail)
async def archive(workflow_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    principal.require(Permission.WORKFLOWS_PUBLISH, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    await svc.set_archived(session, principal, wf, True)
    return await _detail(session, wf)


@router.post("/workspaces/{workspace_id}/workflows/{workflow_id}/restore", response_model=WorkflowDetail)
async def restore(workflow_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    principal.require(Permission.WORKFLOWS_PUBLISH, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    await svc.set_archived(session, principal, wf, False)
    return await _detail(session, wf)


@router.delete("/workspaces/{workspace_id}/workflows/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    principal.require(Permission.WORKFLOWS_DELETE, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    active = (
        await session.execute(
            select(func.count())
            .select_from(Execution)
            .where(Execution.workflow_id == wf.id, Execution.status.notin_(["COMPLETED", "FAILED", "CANCELLED"]))
        )
    ).scalar_one()
    if active:
        raise ValidationFailedError(f"Workflow has {active} active executions; cancel them or archive instead")
    await audit.record(
        session,
        principal,
        "workflow.deleted",
        resource_type="workflow",
        resource_id=wf.id,
        workspace_id=ws.id,
        summary=f"Workflow '{wf.name}' deleted",
    )
    await session.delete(wf)


# ----------------------------------------------------------------------------- trigger info


@router.get("/workspaces/{workspace_id}/workflows/{workflow_id}/trigger")
async def trigger_info(
    workflow_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep, reveal_secret: bool = False
) -> dict[str, Any]:
    principal.require(Permission.WORKFLOWS_READ, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    trig = (
        await session.execute(select(WorkflowTrigger).where(WorkflowTrigger.workflow_id == wf.id))
    ).scalar_one_or_none()
    if trig is None:
        return {"active": False, "message": "Publish the workflow to activate its trigger"}
    info: dict[str, Any] = {
        "active": trig.enabled,
        "trigger_type": trig.trigger_type,
        "node_id": trig.node_id,
        "next_run_at": trig.next_run_at,
        "last_run_at": trig.last_run_at,
        "last_error": trig.last_error,
        "event_name": trig.event_name,
    }
    if trig.webhook_token:
        info["webhook_url"] = f"{get_settings().public_base_url.rstrip('/')}/api/v1/hooks/{trig.webhook_token}"
        info["authentication"] = trig.config.get("authentication", "signature")
        info["signature_header"] = "X-FlowForge-Signature"
        info["timestamp_header"] = "X-FlowForge-Timestamp"
        if trig.signing_secret_id:
            info["signing_secret"] = "whsec_••••••••"
            if reveal_secret:
                principal.require(Permission.WORKFLOWS_PUBLISH, ws.id)
                info["signing_secret"] = (await read_secret(session, trig.signing_secret_id, wf.org_id))[
                    "signing_secret"
                ]
                await audit.record(
                    session,
                    principal,
                    "workflow.webhook_secret_revealed",
                    resource_type="workflow",
                    resource_id=wf.id,
                    workspace_id=ws.id,
                    summary="Webhook signing secret revealed",
                )
    return info


@router.post("/workspaces/{workspace_id}/workflows/{workflow_id}/trigger/rotate-secret")
async def rotate_webhook_secret(
    workflow_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep
) -> dict[str, str]:
    principal.require(Permission.WORKFLOWS_PUBLISH, ws.id)
    wf = await svc.get_workflow(session, principal, workflow_id, ws.id)
    trig = (
        await session.execute(select(WorkflowTrigger).where(WorkflowTrigger.workflow_id == wf.id))
    ).scalar_one_or_none()
    if trig is None or trig.trigger_type != "webhook":
        raise ValidationFailedError("Workflow has no webhook trigger")
    new_secret = "whsec_" + secrets.token_urlsafe(32)
    existing = await session.get(Secret, trig.signing_secret_id) if trig.signing_secret_id else None
    secret = await write_secret(session, wf.org_id, {"signing_secret": new_secret}, existing)
    trig.signing_secret_id = secret.id
    await audit.record(
        session,
        principal,
        "workflow.webhook_secret_rotated",
        resource_type="workflow",
        resource_id=wf.id,
        workspace_id=ws.id,
        summary="Webhook signing secret rotated",
    )
    return {"signing_secret": new_secret}
