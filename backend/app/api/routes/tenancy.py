"""Organisation, workspace, user, team, role and API-key management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import PrincipalDep, SessionDep, require
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailedError
from app.core.principal import Principal
from app.core.rbac import ORG_SCOPED_ONLY, Permission, can_grant
from app.core.security import hash_password, validate_password_strength
from app.db.enums import Role
from app.db.models import ApiKey, Organization, RoleAssignment, Team, TeamMember, User, Workspace
from app.schemas.common import Page
from app.schemas.tenancy import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    OrganizationOut,
    OrganizationUpdate,
    RoleGrant,
    TeamIn,
    TeamMembersUpdate,
    TeamOut,
    UserCreate,
    UserOut,
    UserUpdate,
    WorkspaceIn,
    WorkspaceOut,
)
from app.services import audit
from app.services import auth as auth_service
from app.services.auth import slugify

router = APIRouter(tags=["tenancy"])


# --------------------------------------------------------------------------- organisation


@router.get("/orgs/current", response_model=OrganizationOut)
async def get_org(principal: PrincipalDep, session: SessionDep):
    return await session.get(Organization, principal.org_id)


@router.patch("/orgs/current", response_model=OrganizationOut)
async def update_org(
    body: OrganizationUpdate,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.ORG_MANAGE))],
):
    org = await session.get(Organization, principal.org_id)
    assert org is not None
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes:
        org.name = changes["name"]
    if "settings" in changes:
        org.settings = {**org.settings, **changes["settings"]}
    await audit.record(
        session,
        principal,
        "org.updated",
        resource_type="organization",
        resource_id=org.id,
        summary="Organization settings updated",
        details={"fields": sorted(changes)},
    )
    return org


# --------------------------------------------------------------------------- workspaces


@router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_workspaces(principal: PrincipalDep, session: SessionDep):
    stmt = select(Workspace).where(Workspace.org_id == principal.org_id).order_by(Workspace.created_at)
    allowed = principal.workspace_filter()
    if allowed is not None:
        stmt = stmt.where(Workspace.id.in_(allowed or [uuid.UUID(int=0)]))
    return (await session.execute(stmt)).scalars().all()


@router.post("/workspaces", response_model=WorkspaceOut, status_code=201)
async def create_workspace(
    body: WorkspaceIn,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.WORKSPACES_MANAGE))],
):
    ws = Workspace(
        org_id=principal.org_id, name=body.name, slug=body.slug or slugify(body.name), description=body.description
    )
    session.add(ws)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("Workspace slug already exists") from exc
    await audit.record(
        session,
        principal,
        "workspace.created",
        resource_type="workspace",
        resource_id=ws.id,
        workspace_id=ws.id,
        summary=f"Workspace '{ws.name}' created",
    )
    return ws


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: uuid.UUID,
    body: WorkspaceIn,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.WORKSPACES_MANAGE))],
):
    ws = await _get_ws(session, principal, workspace_id)
    ws.name = body.name
    ws.description = body.description
    if body.slug:
        ws.slug = body.slug
    await audit.record(
        session,
        principal,
        "workspace.updated",
        resource_type="workspace",
        resource_id=ws.id,
        workspace_id=ws.id,
        summary=f"Workspace '{ws.name}' updated",
    )
    return ws


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: uuid.UUID,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.WORKSPACES_MANAGE))],
):
    ws = await _get_ws(session, principal, workspace_id)
    count = (
        await session.execute(select(func.count()).select_from(Workspace).where(Workspace.org_id == principal.org_id))
    ).scalar_one()
    if count <= 1:
        raise ValidationFailedError("Cannot delete the last workspace")
    await session.delete(ws)
    await audit.record(
        session,
        principal,
        "workspace.deleted",
        resource_type="workspace",
        resource_id=ws.id,
        summary=f"Workspace '{ws.name}' deleted",
    )


async def _get_ws(session, principal: Principal, workspace_id: uuid.UUID) -> Workspace:
    ws = (
        await session.execute(
            select(Workspace).where(Workspace.id == workspace_id, Workspace.org_id == principal.org_id)
        )
    ).scalar_one_or_none()
    if ws is None:
        raise NotFoundError("Workspace not found")
    return ws


# --------------------------------------------------------------------------- users & roles


@router.get("/users", response_model=Page[UserOut])
async def list_users(
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.USERS_READ))],
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    stmt = select(User).where(User.org_id == principal.org_id)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(func.lower(User.email).like(like) | func.lower(User.full_name).like(like))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.order_by(User.created_at).limit(limit).offset(offset))).scalars().all()
    return Page(items=[UserOut.model_validate(u) for u in rows], total=total, limit=limit, offset=offset)


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreate,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.USERS_MANAGE))],
):
    _check_grant(principal, body.role, body.workspace_id)
    try:
        validate_password_strength(body.password)
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from exc
    if body.workspace_id:
        await _get_ws(session, principal, body.workspace_id)
    email = body.email.lower()
    if (await session.execute(select(User.id).where(User.email == email))).first():
        raise ConflictError("A user with this email already exists")
    user = User(
        org_id=principal.org_id, email=email, full_name=body.full_name, password_hash=hash_password(body.password)
    )
    session.add(user)
    await session.flush()
    session.add(
        RoleAssignment(
            org_id=principal.org_id,
            user_id=user.id,
            role=body.role.value,
            workspace_id=body.workspace_id,
            granted_by=principal.user_id,
        )
    )
    await audit.record(
        session,
        principal,
        "user.created",
        resource_type="user",
        resource_id=user.id,
        summary=f"User {email} created with role {body.role.value}",
        details={"role": body.role.value, "workspace_id": str(body.workspace_id or "")},
    )
    await session.flush()
    await session.refresh(user, ["role_assignments"])
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.USERS_MANAGE))],
):
    user = await _get_user(session, principal, user_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("is_active") is False and user.id == principal.user_id:
        raise ValidationFailedError("You cannot deactivate yourself")
    for k, v in changes.items():
        setattr(user, k, v)
    action = "user.deactivated" if changes.get("is_active") is False else "user.updated"
    await audit.record(
        session,
        principal,
        action,
        resource_type="user",
        resource_id=user.id,
        summary=f"User {user.email} updated",
        details={"fields": sorted(changes)},
    )
    return user


@router.post("/users/{user_id}/roles", response_model=UserOut)
async def grant_role(
    user_id: uuid.UUID,
    body: RoleGrant,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.USERS_MANAGE))],
):
    user = await _get_user(session, principal, user_id)
    _check_grant(principal, body.role, body.workspace_id)
    if body.workspace_id:
        await _get_ws(session, principal, body.workspace_id)
    exists = any(ra.role == body.role.value and ra.workspace_id == body.workspace_id for ra in user.role_assignments)
    if not exists:
        user.role_assignments.append(
            RoleAssignment(
                org_id=principal.org_id,
                role=body.role.value,
                workspace_id=body.workspace_id,
                granted_by=principal.user_id,
            )
        )
        await audit.record(
            session,
            principal,
            "user.role_granted",
            resource_type="user",
            resource_id=user.id,
            workspace_id=body.workspace_id,
            summary=f"Granted {body.role.value} to {user.email}",
            details={"role": body.role.value, "workspace_id": str(body.workspace_id or "")},
        )
    await session.flush()
    return user


@router.delete("/users/{user_id}/roles/{assignment_id}", response_model=UserOut)
async def revoke_role(
    user_id: uuid.UUID,
    assignment_id: uuid.UUID,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.USERS_MANAGE))],
):
    user = await _get_user(session, principal, user_id)
    ra = next((r for r in user.role_assignments if r.id == assignment_id), None)
    if ra is None:
        raise NotFoundError("Role assignment not found")
    _check_grant(principal, Role(ra.role), ra.workspace_id)
    if user.id == principal.user_id and ra.role == Role.ORG_ADMIN.value:
        others = (
            await session.execute(
                select(func.count())
                .select_from(RoleAssignment)
                .where(
                    RoleAssignment.org_id == principal.org_id,
                    RoleAssignment.role == Role.ORG_ADMIN.value,
                    RoleAssignment.user_id != user.id,
                )
            )
        ).scalar_one()
        if others == 0:
            raise ValidationFailedError("Cannot remove the last organization admin")
    user.role_assignments.remove(ra)
    await audit.record(
        session,
        principal,
        "user.role_revoked",
        resource_type="user",
        resource_id=user.id,
        workspace_id=ra.workspace_id,
        summary=f"Revoked {ra.role} from {user.email}",
        details={"role": ra.role},
    )
    await session.flush()
    return user


def _check_grant(principal: Principal, role: Role, workspace_id: uuid.UUID | None) -> None:
    if not can_grant(principal.roles_for(workspace_id), role):
        raise PermissionDeniedError(f"You cannot grant the role '{role.value}'")
    if role in ORG_SCOPED_ONLY and workspace_id is not None:
        raise ValidationFailedError(f"Role '{role.value}' can only be granted at organization scope")


async def _get_user(session, principal: Principal, user_id: uuid.UUID) -> User:
    user = (
        await session.execute(select(User).where(User.id == user_id, User.org_id == principal.org_id))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found")
    return user


# --------------------------------------------------------------------------- teams


async def _team_out(session, team: Team) -> TeamOut:
    members = (await session.execute(select(TeamMember.user_id).where(TeamMember.team_id == team.id))).scalars().all()
    out = TeamOut.model_validate(team)
    out.member_ids = list(members)
    return out


@router.get("/teams", response_model=list[TeamOut])
async def list_teams(principal: PrincipalDep, session: SessionDep):
    teams = (
        (await session.execute(select(Team).where(Team.org_id == principal.org_id).order_by(Team.name))).scalars().all()
    )
    return [await _team_out(session, t) for t in teams]


@router.post("/teams", response_model=TeamOut, status_code=201)
async def create_team(
    body: TeamIn,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.TEAMS_MANAGE))],
):
    if body.workspace_id:
        await _get_ws(session, principal, body.workspace_id)
    team = Team(org_id=principal.org_id, name=body.name, description=body.description, workspace_id=body.workspace_id)
    session.add(team)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("Team name already exists") from exc
    await audit.record(
        session,
        principal,
        "team.created",
        resource_type="team",
        resource_id=team.id,
        summary=f"Team '{team.name}' created",
    )
    return await _team_out(session, team)


@router.post("/teams/{team_id}/members", response_model=TeamOut)
async def update_team_members(
    team_id: uuid.UUID,
    body: TeamMembersUpdate,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.TEAMS_MANAGE))],
):
    team = (
        await session.execute(select(Team).where(Team.id == team_id, Team.org_id == principal.org_id))
    ).scalar_one_or_none()
    if team is None:
        raise NotFoundError("Team not found")
    if body.add:
        valid = set(
            (await session.execute(select(User.id).where(User.id.in_(body.add), User.org_id == principal.org_id)))
            .scalars()
            .all()
        )
        if valid != set(body.add):
            raise NotFoundError("One or more users not found")
        existing = set(
            (await session.execute(select(TeamMember.user_id).where(TeamMember.team_id == team.id))).scalars().all()
        )
        for uid in valid - existing:
            session.add(TeamMember(team_id=team.id, user_id=uid, org_id=principal.org_id))
    if body.remove:
        await session.execute(
            delete(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id.in_(body.remove))
        )
    await session.flush()
    await audit.record(
        session,
        principal,
        "team.members_updated",
        resource_type="team",
        resource_id=team.id,
        summary=f"Team '{team.name}' membership updated",
        details={"added": [str(u) for u in body.add], "removed": [str(u) for u in body.remove]},
    )
    return await _team_out(session, team)


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team(
    team_id: uuid.UUID,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.TEAMS_MANAGE))],
):
    team = (
        await session.execute(select(Team).where(Team.id == team_id, Team.org_id == principal.org_id))
    ).scalar_one_or_none()
    if team is None:
        raise NotFoundError("Team not found")
    await session.delete(team)
    await audit.record(
        session,
        principal,
        "team.deleted",
        resource_type="team",
        resource_id=team.id,
        summary=f"Team '{team.name}' deleted",
    )


# --------------------------------------------------------------------------- API keys


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    session: SessionDep, principal: Annotated[Principal, Depends(require(Permission.API_KEYS_MANAGE))]
):
    return (
        (
            await session.execute(
                select(ApiKey).where(ApiKey.org_id == principal.org_id).order_by(ApiKey.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201)
async def create_api_key(
    body: ApiKeyCreate,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.API_KEYS_MANAGE))],
):
    _check_grant(principal, body.role, body.workspace_id)
    if body.workspace_id:
        await _get_ws(session, principal, body.workspace_id)
    key, raw = await auth_service.create_api_key(
        session, principal, name=body.name, role=body.role, workspace_id=body.workspace_id, expires_at=body.expires_at
    )
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), key=raw)


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: uuid.UUID,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.API_KEYS_MANAGE))],
):
    key = (
        await session.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.org_id == principal.org_id))
    ).scalar_one_or_none()
    if key is None:
        raise NotFoundError("API key not found")
    key.revoked_at = datetime.now(UTC)
    await audit.record(
        session,
        principal,
        "api_key.revoked",
        resource_type="api_key",
        resource_id=key.id,
        summary=f"API key '{key.name}' revoked",
    )
