"""Authenticated principal and its tenant-scoped authorisation helpers."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.core.errors import PermissionDeniedError
from app.core.rbac import Permission, permissions_for
from app.db.enums import ActorType, Role


@dataclass
class Principal:
    actor_type: ActorType
    org_id: uuid.UUID
    user_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None
    email: str | None = None
    name: str | None = None
    is_super_admin: bool = False
    org_roles: set[Role] = field(default_factory=set)
    workspace_roles: dict[uuid.UUID, set[Role]] = field(default_factory=dict)
    team_ids: set[uuid.UUID] = field(default_factory=set)

    @property
    def actor_id(self) -> uuid.UUID | None:
        return self.user_id or self.api_key_id

    def roles_for(self, workspace_id: uuid.UUID | None = None) -> set[Role]:
        roles = set(self.org_roles)
        if self.is_super_admin:
            roles.add(Role.SUPER_ADMIN)
        if workspace_id is not None:
            roles |= self.workspace_roles.get(workspace_id, set())
        return roles

    def all_roles(self) -> set[Role]:
        roles = self.roles_for(None)
        for ws_roles in self.workspace_roles.values():
            roles |= ws_roles
        return roles

    def has(self, permission: Permission, workspace_id: uuid.UUID | None = None) -> bool:
        return permission in permissions_for(self.roles_for(workspace_id))

    def has_anywhere(self, permission: Permission) -> bool:
        if self.has(permission):
            return True
        return any(permission in permissions_for(self.roles_for(ws)) for ws in self.workspace_roles)

    def require(self, permission: Permission, workspace_id: uuid.UUID | None = None) -> None:
        if not self.has(permission, workspace_id):
            raise PermissionDeniedError(f"Missing permission '{permission.value}'")

    def can_access_workspace(self, workspace_id: uuid.UUID) -> bool:
        return bool(self.org_roles) or self.is_super_admin or workspace_id in self.workspace_roles

    def workspace_filter(self) -> set[uuid.UUID] | None:
        """``None`` means every workspace in the org; otherwise the explicit allow-list."""
        if self.org_roles or self.is_super_admin:
            return None
        return set(self.workspace_roles)

    def workspaces_with(self, permission: Permission) -> set[uuid.UUID] | None:
        """Workspaces in which the principal holds ``permission`` (``None`` = all workspaces)."""
        if self.has(permission):
            return None
        return {ws for ws in self.workspace_roles if permission in permissions_for(self.roles_for(ws))}


@dataclass
class RequestMeta:
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


request_meta_var: ContextVar[RequestMeta | None] = ContextVar("request_meta", default=None)


def system_principal(org_id: uuid.UUID) -> Principal:
    """Principal used by the engine and schedulers acting on behalf of a tenant."""
    return Principal(actor_type=ActorType.SYSTEM, org_id=org_id, org_roles={Role.ORG_ADMIN}, name="system")
