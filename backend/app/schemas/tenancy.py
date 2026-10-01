from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from app.db.enums import Role
from app.schemas.common import ORMModel


class RegisterOrgRequest(BaseModel):
    organization_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=512)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class OrganizationOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    plan: str
    settings: dict[str, Any]
    created_at: datetime


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    settings: dict[str, Any] | None = None


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,78}$")
    description: str = Field(default="", max_length=2000)


class WorkspaceOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str
    created_at: datetime


class RoleAssignmentOut(ORMModel):
    id: uuid.UUID
    role: Role
    workspace_id: uuid.UUID | None


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    is_super_admin: bool
    last_login_at: datetime | None
    created_at: datetime
    role_assignments: list[RoleAssignmentOut] = []


class MeOut(BaseModel):
    user: UserOut | None
    organization: OrganizationOut
    actor_type: str
    roles: list[str]
    workspace_roles: dict[str, list[str]]
    permissions: list[str]
    team_ids: list[uuid.UUID]


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=256)
    role: Role = Role.VIEWER
    workspace_id: uuid.UUID | None = None


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None


class RoleGrant(BaseModel):
    role: Role
    workspace_id: uuid.UUID | None = None


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    workspace_id: uuid.UUID | None = None


class TeamOut(ORMModel):
    id: uuid.UUID
    name: str
    description: str
    workspace_id: uuid.UUID | None
    created_at: datetime
    member_ids: list[uuid.UUID] = []


class TeamMembersUpdate(BaseModel):
    add: list[uuid.UUID] = []
    remove: list[uuid.UUID] = []


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: Role = Role.OPERATOR
    workspace_id: uuid.UUID | None = None
    expires_at: datetime | None = None


class ApiKeyOut(ORMModel):
    id: uuid.UUID
    name: str
    prefix: str
    role: str
    workspace_id: uuid.UUID | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str


class AuditLogOut(ORMModel):
    id: int
    actor_type: str
    actor_id: uuid.UUID | None
    actor_email: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    workspace_id: uuid.UUID | None
    summary: str
    outcome: str
    ip_address: str | None
    user_agent: str | None
    request_id: str | None
    details: dict[str, Any]
    created_at: datetime
