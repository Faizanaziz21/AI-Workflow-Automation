"""Role-based access control: the single source of truth for who may do what."""

from __future__ import annotations

from enum import StrEnum

from app.db.enums import Role


class Permission(StrEnum):
    # organisation administration
    ORG_MANAGE = "org:manage"
    USERS_READ = "users:read"
    USERS_MANAGE = "users:manage"
    TEAMS_MANAGE = "teams:manage"
    WORKSPACES_MANAGE = "workspaces:manage"
    API_KEYS_MANAGE = "api_keys:manage"
    AUDIT_READ = "audit:read"
    # workflows
    WORKFLOWS_READ = "workflows:read"
    WORKFLOWS_WRITE = "workflows:write"
    WORKFLOWS_PUBLISH = "workflows:publish"
    WORKFLOWS_DELETE = "workflows:delete"
    # executions
    EXECUTIONS_READ = "executions:read"
    EXECUTIONS_RUN = "executions:run"
    EXECUTIONS_OPERATE = "executions:operate"  # cancel, pause, resume, retry, replay
    EXECUTIONS_READ_SENSITIVE = "executions:read_sensitive"
    # connections / secrets
    CONNECTIONS_READ = "connections:read"
    CONNECTIONS_USE = "connections:use"
    CONNECTIONS_MANAGE = "connections:manage"
    # approvals
    APPROVALS_DECIDE = "approvals:decide"
    APPROVALS_READ = "approvals:read"
    # AI & ops
    AI_PROMPTS_MANAGE = "ai_prompts:manage"
    DASHBOARD_READ = "dashboard:read"
    DEAD_LETTERS_MANAGE = "dead_letters:manage"
    FILES_WRITE = "files:write"
    EVENTS_PUBLISH = "events:publish"
    PLATFORM_ADMIN = "platform:admin"


P = Permission

_VIEWER = {
    P.WORKFLOWS_READ,
    P.EXECUTIONS_READ,
    P.CONNECTIONS_READ,
    P.DASHBOARD_READ,
    P.APPROVALS_READ,
    P.USERS_READ,
}
_APPROVER = _VIEWER | {P.APPROVALS_DECIDE}
_OPERATOR = _VIEWER | {
    P.EXECUTIONS_RUN,
    P.EXECUTIONS_OPERATE,
    P.DEAD_LETTERS_MANAGE,
    P.EVENTS_PUBLISH,
    P.FILES_WRITE,
    P.CONNECTIONS_USE,
}
_DEVELOPER = _OPERATOR | {
    P.WORKFLOWS_WRITE,
    P.WORKFLOWS_PUBLISH,
    P.CONNECTIONS_MANAGE,
    P.AI_PROMPTS_MANAGE,
}
_ORG_ADMIN = (
    _DEVELOPER
    | _APPROVER
    | {
        P.ORG_MANAGE,
        P.USERS_MANAGE,
        P.TEAMS_MANAGE,
        P.WORKSPACES_MANAGE,
        P.API_KEYS_MANAGE,
        P.AUDIT_READ,
        P.WORKFLOWS_DELETE,
        P.EXECUTIONS_READ_SENSITIVE,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_VIEWER),
    Role.APPROVER: frozenset(_APPROVER),
    Role.OPERATOR: frozenset(_OPERATOR),
    Role.WORKFLOW_DEVELOPER: frozenset(_DEVELOPER),
    Role.ORG_ADMIN: frozenset(_ORG_ADMIN),
    Role.SUPER_ADMIN: frozenset(Permission),
}

# Higher rank may grant roles of lower-or-equal rank.
ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 10,
    Role.APPROVER: 20,
    Role.OPERATOR: 30,
    Role.WORKFLOW_DEVELOPER: 40,
    Role.ORG_ADMIN: 90,
    Role.SUPER_ADMIN: 100,
}

ORG_SCOPED_ONLY = frozenset({Role.ORG_ADMIN, Role.SUPER_ADMIN})


def permissions_for(roles: set[Role]) -> frozenset[Permission]:
    out: set[Permission] = set()
    for r in roles:
        out |= ROLE_PERMISSIONS[r]
    return frozenset(out)


def can_grant(granter_roles: set[Role], role: Role) -> bool:
    if not granter_roles:
        return False
    top = max(ROLE_RANK[r] for r in granter_roles)
    if role == Role.SUPER_ADMIN:
        return Role.SUPER_ADMIN in granter_roles
    return top >= ROLE_RANK[Role.ORG_ADMIN] and top >= ROLE_RANK[role]
