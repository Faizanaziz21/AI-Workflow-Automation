"""Inbound triggers: signed webhooks, API events, file uploads."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import PrincipalDep, SessionDep, WorkspaceDep
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.rbac import Permission
from app.core.redis import get_redis
from app.db.models import StoredFile
from app.db.session import get_sessionmaker
from app.services import audit
from app.services import triggers as svc
from app.services.storage import file_ref, get_blob_storage, store_file

router = APIRouter(tags=["triggers"])


@router.api_route("/hooks/{token}", methods=["POST", "PUT", "GET"], include_in_schema=True)
async def webhook(token: str, request: Request) -> Response:
    """Public webhook endpoint. Authentication is per-trigger (HMAC signature or API key)."""
    body = await request.body()
    if len(body) > get_settings().max_upload_bytes:
        raise ValidationFailedError("Payload too large")
    async with get_sessionmaker()() as session:
        ex, created, trig = await svc.ingest_webhook(
            session,
            token,
            method=request.method,
            headers=dict(request.headers),
            query=dict(request.query_params),
            body_bytes=body,
        )
        await session.commit()
    if (trig.config or {}).get("response_mode") == "wait_for_response" and created:
        timeout = get_settings().webhook_sync_timeout_seconds
        try:
            item = await get_redis().blpop([f"webhook:response:{ex.id}"], timeout=timeout)
        except Exception:
            item = None
        if item:
            resp = json.loads(item[1])
            return JSONResponse(
                resp.get("body"),
                status_code=int(resp.get("status_code", 200)),
                headers={**resp.get("headers", {}), "X-FlowForge-Execution-Id": str(ex.id)},
            )
        return JSONResponse(
            {
                "execution_id": str(ex.id),
                "status": "RUNNING",
                "message": "Workflow still running; poll the execution for its result",
            },
            status_code=202,
        )
    return JSONResponse(
        {"execution_id": str(ex.id), "status": ex.status, "duplicate": not created},
        status_code=202 if created else 200,
        headers={"X-FlowForge-Execution-Id": str(ex.id)},
    )


class EventBody(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,200}$")
    data: Any = None
    event_id: str | None = Field(default=None, max_length=150, description="Idempotency key for the event")
    workspace_id: uuid.UUID | None = None


@router.post("/events", status_code=202)
async def publish_event(body: EventBody, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    if body.workspace_id:
        principal.require(Permission.EVENTS_PUBLISH, body.workspace_id)
    elif not principal.has_anywhere(Permission.EVENTS_PUBLISH):
        principal.require(Permission.EVENTS_PUBLISH)
    results = await svc.publish_event(session, principal, body.name, body.data, body.event_id, body.workspace_id)
    return {"event": body.name, "triggered": results}


@router.post("/workspaces/{workspace_id}/files", status_code=201)
async def upload_file(
    ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep, file: Annotated[UploadFile, File()]
) -> dict[str, Any]:
    principal.require(Permission.FILES_WRITE, ws.id)
    content = await file.read(get_settings().max_upload_bytes + 1)
    if len(content) > get_settings().max_upload_bytes:
        raise ValidationFailedError("File exceeds maximum size")
    record = await store_file(
        session,
        org_id=principal.org_id,
        workspace_id=ws.id,
        filename=file.filename or "upload",
        content=content,
        content_type=file.content_type or "application/octet-stream",
        source="upload",
        created_by=principal.user_id,
    )
    started = await svc.dispatch_file_uploaded(session, record, principal.user_id)
    await audit.record(
        session,
        principal,
        "file.uploaded",
        resource_type="file",
        resource_id=record.id,
        workspace_id=ws.id,
        summary=f"Uploaded {record.filename} ({record.size_bytes} bytes)",
    )
    return {**file_ref(record), "triggered_executions": started}


@router.get("/workspaces/{workspace_id}/files")
async def list_files(ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    principal.require(Permission.WORKFLOWS_READ, ws.id)
    rows = (
        (
            await session.execute(
                select(StoredFile)
                .where(StoredFile.org_id == principal.org_id, StoredFile.workspace_id == ws.id)
                .order_by(StoredFile.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [{**file_ref(f), "source": f.source, "created_at": f.created_at.isoformat()} for f in rows]


@router.get("/workspaces/{workspace_id}/files/{file_id}/download")
async def download_file(file_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep) -> Response:
    principal.require(Permission.EXECUTIONS_READ, ws.id)
    f = (
        await session.execute(
            select(StoredFile).where(
                StoredFile.id == file_id, StoredFile.org_id == principal.org_id, StoredFile.workspace_id == ws.id
            )
        )
    ).scalar_one_or_none()
    if f is None:
        raise NotFoundError("File not found")
    data = await asyncio.wait_for(get_blob_storage().get(f.storage_key), timeout=30)
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{f.filename}"', "X-Content-Type-Options": "nosniff"},
    )
