"""Connector catalog, connections (credentials are write-only), OAuth, and secret administration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import PrincipalDep, SessionDep, WorkspaceDep, require
from app.connectors.registry import all_connectors, get_connector
from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.principal import Principal
from app.core.rbac import Permission
from app.db.models import Connection
from app.schemas.common import ORMModel
from app.services import connections as svc

router = APIRouter(tags=["connections"])


class AccessPolicy(BaseModel):
    roles: list[str] = []
    team_ids: list[uuid.UUID] = []


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    connector_key: str
    config: dict[str, Any] = {}
    credentials: dict[str, Any] = {}
    access_policy: AccessPolicy = AccessPolicy()


class ConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, Any] | None = None
    credentials: dict[str, Any] | None = None
    access_policy: AccessPolicy | None = None


class ConnectionOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    connector_key: str
    auth_type: str
    config: dict[str, Any]
    status: str
    status_message: str | None
    last_tested_at: datetime | None
    credentials_expire_at: datetime | None
    access_policy: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    credential_fields: list[str] = []
    secret_version: int | None = None
    can_use: bool = False


class TestResultOut(BaseModel):
    ok: bool
    message: str
    details: dict[str, Any] = {}


async def _out(session, principal: Principal, conn: Connection) -> ConnectionOut:
    out = ConnectionOut.model_validate(conn)
    creds = await svc.read_secret(session, conn.secret_id, conn.org_id)
    out.credential_fields = svc.credential_fields_set(creds)  # names only — values never leave the server
    if conn.secret_id:
        from app.db.models import Secret

        secret = await session.get(Secret, conn.secret_id)
        out.secret_version = secret.version if secret else None
    out.can_use = svc.can_use_connection(principal, conn)
    return out


@router.get("/connectors")
async def list_connectors(principal: PrincipalDep, category: str | None = None) -> list[dict[str, Any]]:
    return [c.describe() for c in all_connectors() if category is None or c.category == category]


@router.get("/connectors/{key}")
async def get_connector_detail(key: str, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return get_connector(key).describe()
    except KeyError as exc:
        raise NotFoundError("Connector not found") from exc


@router.get("/workspaces/{workspace_id}/connections", response_model=list[ConnectionOut])
async def list_connections(
    ws: WorkspaceDep,
    principal: PrincipalDep,
    session: SessionDep,
    connector_key: str | None = None,
    category: Annotated[str | None, Query()] = None,
):
    principal.require(Permission.CONNECTIONS_READ, ws.id)
    stmt = select(Connection).where(Connection.org_id == principal.org_id, Connection.workspace_id == ws.id)
    if connector_key:
        stmt = stmt.where(Connection.connector_key == connector_key)
    rows = (await session.execute(stmt.order_by(Connection.name))).scalars().all()
    if category:
        rows = [r for r in rows if _category(r.connector_key) == category]
    return [await _out(session, principal, r) for r in rows]


def _category(key: str) -> str | None:
    try:
        return get_connector(key).category
    except KeyError:
        return None


@router.post("/workspaces/{workspace_id}/connections", response_model=ConnectionOut, status_code=201)
async def create_connection(body: ConnectionCreate, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    principal.require(Permission.CONNECTIONS_MANAGE, ws.id)
    try:
        conn = await svc.create_connection(
            session,
            principal,
            workspace_id=ws.id,
            name=body.name,
            connector_key=body.connector_key,
            config=body.config,
            credentials=body.credentials,
            access_policy=body.access_policy.model_dump(mode="json"),
        )
    except IntegrityError as exc:
        raise ConflictError("A connection with this name already exists in the workspace") from exc
    return await _out(session, principal, conn)


@router.get("/workspaces/{workspace_id}/connections/{connection_id}", response_model=ConnectionOut)
async def get_connection(connection_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    principal.require(Permission.CONNECTIONS_READ, ws.id)
    conn = await svc.get_connection(session, principal.org_id, connection_id, ws.id)
    return await _out(session, principal, conn)


@router.patch("/workspaces/{workspace_id}/connections/{connection_id}", response_model=ConnectionOut)
async def update_connection(
    connection_id: uuid.UUID, body: ConnectionUpdate, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep
):
    principal.require(Permission.CONNECTIONS_MANAGE, ws.id)
    conn = await svc.get_connection(session, principal.org_id, connection_id, ws.id)
    await svc.update_connection(
        session,
        principal,
        conn,
        name=body.name,
        config=body.config,
        credentials=body.credentials,
        access_policy=body.access_policy.model_dump(mode="json") if body.access_policy else None,
    )
    return await _out(session, principal, conn)


@router.post("/workspaces/{workspace_id}/connections/{connection_id}/rotate", response_model=ConnectionOut)
async def rotate_credentials(
    connection_id: uuid.UUID, body: dict[str, Any], ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep
):
    """Replace credentials (new encrypted version); the previous ciphertext is overwritten."""
    principal.require(Permission.CONNECTIONS_MANAGE, ws.id)
    conn = await svc.get_connection(session, principal.org_id, connection_id, ws.id)
    await svc.update_connection(session, principal, conn, credentials=body.get("credentials") or body)
    return await _out(session, principal, conn)


@router.delete("/workspaces/{workspace_id}/connections/{connection_id}", status_code=204)
async def delete_connection(connection_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    principal.require(Permission.CONNECTIONS_MANAGE, ws.id)
    conn = await svc.get_connection(session, principal.org_id, connection_id, ws.id)
    await svc.delete_connection(session, principal, conn)


@router.post("/workspaces/{workspace_id}/connections/{connection_id}/test", response_model=TestResultOut)
async def test_connection(connection_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    conn = await svc.get_connection(session, principal.org_id, connection_id, ws.id)
    if not svc.can_use_connection(principal, conn):
        raise PermissionDeniedError("Not allowed to use this connection")
    result = await svc.run_connection_test(session, principal, conn)
    return TestResultOut(ok=result.ok, message=result.message, details=result.details)


@router.post("/workspaces/{workspace_id}/connections/{connection_id}/oauth/start")
async def oauth_start(connection_id: uuid.UUID, ws: WorkspaceDep, principal: PrincipalDep, session: SessionDep):
    conn = await svc.get_connection(session, principal.org_id, connection_id, ws.id)
    return {"authorize_url": await svc.oauth_start(session, principal, conn)}


@router.get("/connections/oauth/callback", include_in_schema=False)
async def oauth_callback(state: str, session: SessionDep, code: str | None = None, error: str | None = None):
    frontend = get_settings().cors_origins[0] if get_settings().cors_origins else ""
    if error or not code:
        return RedirectResponse(f"{frontend}/connections?oauth=error", status_code=302)
    conn = await svc.oauth_callback(session, state, code)
    return RedirectResponse(f"{frontend}/connections?oauth=success&connection={conn.id}", status_code=302)


@router.post("/admin/secrets/rewrap")
async def rewrap_secrets(session: SessionDep, principal: Annotated[Principal, Depends(require(Permission.ORG_MANAGE))]):
    return {"rewrapped": await svc.rewrap_org_secrets(session, principal)}
