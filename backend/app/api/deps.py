"""FastAPI dependencies: DB session, authentication, permission checks, workspace resolution."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header, Path, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError, NotFoundError, PermissionDeniedError
from app.core.logging import org_id_var
from app.core.principal import Principal
from app.core.rbac import Permission
from app.core.security import decode_access_token
from app.db.models import Workspace
from app.db.session import get_sessionmaker
from app.services import auth as auth_service


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_principal(
    request: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal:
    if x_api_key:
        principal = await auth_service.authenticate_api_key(session, x_api_key)
    elif authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        try:
            claims = decode_access_token(token)
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Access token expired", code="token_expired") from exc
        except jwt.PyJWTError as exc:
            raise AuthenticationError("Invalid access token") from exc
        if claims.session_family and not await auth_service.is_session_family_active(session, claims.session_family):
            raise AuthenticationError("Session revoked")
        principal = await auth_service.load_user_principal(session, claims.user_id, claims.org_id)
    else:
        raise AuthenticationError("Authentication required")
    request.state.principal = principal
    org_id_var.set(str(principal.org_id))
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require(permission: Permission) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """Require an org-wide permission (or the permission in at least one workspace for read-type checks)."""

    async def _dep(principal: PrincipalDep) -> Principal:
        principal.require(permission)
        return principal

    return _dep


def require_anywhere(permission: Permission) -> Callable[..., Coroutine[Any, Any, Principal]]:
    async def _dep(principal: PrincipalDep) -> Principal:
        if not principal.has_anywhere(permission):
            raise PermissionDeniedError(f"Missing permission '{permission.value}'")
        return principal

    return _dep


async def get_workspace(
    workspace_id: Annotated[uuid.UUID, Path()], principal: PrincipalDep, session: SessionDep
) -> Workspace:
    ws = (
        await session.execute(
            select(Workspace).where(Workspace.id == workspace_id, Workspace.org_id == principal.org_id)
        )
    ).scalar_one_or_none()
    if ws is None or not principal.can_access_workspace(ws.id):
        raise NotFoundError("Workspace not found")
    return ws


WorkspaceDep = Annotated[Workspace, Depends(get_workspace)]
