"""Authentication endpoints.

Browser clients receive the refresh token as an ``HttpOnly; SameSite=Strict`` cookie scoped to
``/api/v1/auth`` plus a readable ``ff_csrf`` cookie; cookie-based refresh/logout requires the
``X-CSRF-Token`` header to match (double-submit). API clients may pass the refresh token in the body instead.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Cookie, Header, Request, Response
from sqlalchemy import select

from app.api.deps import PrincipalDep, SessionDep
from app.api.middleware import client_ip
from app.core.config import get_settings
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.rbac import permissions_for
from app.core.security import generate_token
from app.db.models import Organization, User
from app.schemas.tenancy import (
    LoginRequest,
    MeOut,
    OrganizationOut,
    RefreshRequest,
    RegisterOrgRequest,
    TokenResponse,
    UserOut,
)
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "ff_refresh"
CSRF_COOKIE = "ff_csrf"


def _set_auth_cookies(response: Response, refresh_token: str) -> None:
    settings = get_settings()
    secure = settings.is_production
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/api/v1/auth",
    )
    response.set_cookie(
        CSRF_COOKIE,
        generate_token(16),
        max_age=settings.refresh_token_ttl_seconds,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )


def _resolve_refresh(
    body: RefreshRequest | None, cookie_token: str | None, csrf_cookie: str | None, csrf_header: str | None
) -> str:
    if body and body.refresh_token:
        return body.refresh_token
    if cookie_token:
        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            raise PermissionDeniedError("CSRF token missing or invalid", code="csrf_failed")
        return cookie_token
    raise AuthenticationError("Refresh token required")


@router.post("/register-org", response_model=TokenResponse, status_code=201)
async def register_org(body: RegisterOrgRequest, request: Request, response: Response, session: SessionDep):
    await auth_service.register_organization(
        session, org_name=body.organization_name, email=body.email, full_name=body.full_name, password=body.password
    )
    await session.flush()
    pair = await auth_service.login(
        session,
        email=body.email,
        password=body.password,
        user_agent=request.headers.get("user-agent"),
        ip=client_ip(request),
    )
    _set_auth_cookies(response, pair.refresh_token)
    return TokenResponse(**pair.__dict__)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, response: Response, session: SessionDep):
    pair = await auth_service.login(
        session,
        email=body.email,
        password=body.password,
        user_agent=request.headers.get("user-agent"),
        ip=client_ip(request),
    )
    _set_auth_cookies(response, pair.refresh_token)
    return TokenResponse(**pair.__dict__)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
    body: RefreshRequest | None = None,
    ff_refresh: Annotated[str | None, Cookie()] = None,
    ff_csrf: Annotated[str | None, Cookie()] = None,
    x_csrf_token: Annotated[str | None, Header()] = None,
):
    raw = _resolve_refresh(body, ff_refresh, ff_csrf, x_csrf_token)
    pair = await auth_service.refresh(session, raw, user_agent=request.headers.get("user-agent"), ip=client_ip(request))
    _set_auth_cookies(response, pair.refresh_token)
    return TokenResponse(**pair.__dict__)


@router.post("/logout", status_code=204)
async def logout(
    principal: PrincipalDep,
    response: Response,
    session: SessionDep,
    body: RefreshRequest | None = None,
    ff_refresh: Annotated[str | None, Cookie()] = None,
    ff_csrf: Annotated[str | None, Cookie()] = None,
    x_csrf_token: Annotated[str | None, Header()] = None,
):
    raw: str | None = None
    if (body and body.refresh_token) or ff_refresh:
        raw = _resolve_refresh(body, ff_refresh, ff_csrf, x_csrf_token)
    await auth_service.logout(session, principal, raw)
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    response.delete_cookie(CSRF_COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=MeOut)
async def me(principal: PrincipalDep, session: SessionDep):
    org = await session.get(Organization, principal.org_id)
    user = None
    if principal.user_id:
        user = (await session.execute(select(User).where(User.id == principal.user_id))).scalar_one()
    perms = set(permissions_for(principal.roles_for(None)))
    for ws in principal.workspace_roles:
        perms |= permissions_for(principal.roles_for(ws))
    return MeOut(
        user=UserOut.model_validate(user) if user else None,
        organization=OrganizationOut.model_validate(org),
        actor_type=principal.actor_type.value,
        roles=sorted(r.value for r in principal.roles_for(None)),
        workspace_roles={str(k): sorted(r.value for r in v) for k, v in principal.workspace_roles.items()},
        permissions=sorted(p.value for p in perms),
        team_ids=sorted(principal.team_ids),
    )
