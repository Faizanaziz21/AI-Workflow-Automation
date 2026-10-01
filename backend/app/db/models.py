"""SQLAlchemy ORM models.

Every tenant-owned table carries ``org_id``; data access goes through tenant-scoped
repositories (see ``app.services.tenancy``) which always filter on it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin, utcnow, uuid7

UUIDType = PG_UUID(as_uuid=True)


def _org_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)


# --------------------------------------------------------------------------- tenancy


class Organization(IdMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(String(40), nullable=False, default="enterprise")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Workspace(IdMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("org_id", "slug"),)

    org_id: Mapped[uuid.UUID] = _org_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    org_id: Mapped[uuid.UUID] = _org_fk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_super_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auth_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="password")
    external_id: Mapped[str | None] = mapped_column(String(255))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    role_assignments: Mapped[list[RoleAssignment]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class Team(IdMixin, TimestampMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    org_id: Mapped[uuid.UUID] = _org_fk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("workspaces.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class TeamMember(Base):
    __tablename__ = "team_members"

    team_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    org_id: Mapped[uuid.UUID] = _org_fk()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class RoleAssignment(IdMixin, Base):
    """Grants a role to a user at org scope (workspace_id NULL) or for one workspace."""

    __tablename__ = "role_assignments"
    __table_args__ = (
        Index(
            "uq_role_assignment_scope",
            "user_id",
            "role",
            text("coalesce(workspace_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
        ),
    )

    org_id: Mapped[uuid.UUID] = _org_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("workspaces.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    user: Mapped[User] = relationship(back_populates="role_assignments")


class RefreshToken(IdMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[uuid.UUID] = _org_fk()
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    user_agent: Mapped[str | None] = mapped_column(String(500))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ApiKey(IdMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    org_id: Mapped[uuid.UUID] = _org_fk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


AUDIT_TSVECTOR_SQL = (
    "to_tsvector('simple'::regconfig, coalesce(action,'') || ' ' || coalesce(actor_email,'') || ' ' || "
    "coalesce(resource_type,'') || ' ' || coalesce(summary,''))"
)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_org_created", "org_id", "created_at"),
        Index("ix_audit_org_action", "org_id", "action"),
        Index(
            "ix_audit_search",
            text(AUDIT_TSVECTOR_SQL),
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    actor_email: Mapped[str | None] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(60))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


# --------------------------------------------------------------------------- secrets & connections


class Secret(IdMixin, TimestampMixin, Base):
    """Envelope-encrypted credential payload. Plaintext never touches the database."""

    __tablename__ = "secrets"

    org_id: Mapped[uuid.UUID] = _org_fk()
    kek_version: Mapped[str] = mapped_column(String(20), nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class Connection(IdMixin, TimestampMixin, Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    org_id: Mapped[uuid.UUID] = _org_fk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    connector_key: Mapped[str] = mapped_column(String(80), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(40), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    secret_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("secrets.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="untested")
    status_message: Mapped[str | None] = mapped_column(Text)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credentials_expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Access policy: {"roles": [...], "team_ids": [...]} — empty means any member with use permission.
    access_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)


# --------------------------------------------------------------------------- workflows


class Workflow(IdMixin, TimestampMixin, Base):
    __tablename__ = "workflows"
    __table_args__ = (Index("ix_workflows_ws_name", "workspace_id", "name"),)

    org_id: Mapped[uuid.UUID] = _org_fk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(50)), nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    template_slug: Mapped[str | None] = mapped_column(String(100))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)


class WorkflowVersion(IdMixin, Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "version"),
        Index(
            "uq_workflow_single_draft",
            "workflow_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
    )

    org_id: Mapped[uuid.UUID] = _org_fk()
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    change_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)


class WorkflowTrigger(IdMixin, TimestampMixin, Base):
    """Activation record derived from the published version's trigger node."""

    __tablename__ = "workflow_triggers"
    __table_args__ = (
        Index("ix_triggers_due", "next_run_at", postgresql_where=text("enabled AND next_run_at IS NOT NULL")),
        Index("ix_triggers_event", "org_id", "event_name", postgresql_where=text("event_name IS NOT NULL")),
    )

    org_id: Mapped[uuid.UUID] = _org_fk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("workspaces.id", ondelete="CASCADE"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("workflows.id", ondelete="CASCADE"), unique=True
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(30), nullable=False)
    node_id: Mapped[str] = mapped_column(String(100), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    webhook_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    signing_secret_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    event_name: Mapped[str | None] = mapped_column(String(200))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- execution


class Execution(IdMixin, Base):
    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "idempotency_key"),
        Index("ix_exec_org_created", "org_id", "created_at"),
        Index("ix_exec_org_status", "org_id", "status"),
        Index("ix_exec_active_created", "created_at", postgresql_where=text("status IN ('RUNNING', 'RETRYING')")),
        Index("ix_exec_finished", "finished_at", postgresql_where=text("finished_at IS NOT NULL")),
        Index("ix_exec_workflow_created", "workflow_id", "created_at"),
        Index("ix_exec_correlation", "org_id", "correlation_key", postgresql_where=text("correlation_key IS NOT NULL")),
    )

    org_id: Mapped[uuid.UUID] = _org_fk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("workspaces.id", ondelete="CASCADE"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("workflows.id", ondelete="CASCADE"))
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("workflow_versions.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    trigger_type: Mapped[str] = mapped_column(String(30), nullable=False)
    trigger_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    correlation_key: Mapped[str | None] = mapped_column(String(200))
    parent_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class NodeRun(IdMixin, Base):
    """Checkpoint for one node in one scope of one execution."""

    __tablename__ = "node_runs"
    __table_args__ = (
        UniqueConstraint("execution_id", "node_id", "scope"),
        Index("ix_node_runs_wait_key", "org_id", "wait_key", postgresql_where=text("wait_key IS NOT NULL")),
        Index("ix_node_runs_org_finished", "org_id", "finished_at"),
    )

    org_id: Mapped[uuid.UUID] = _org_fk()
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("executions.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[str] = mapped_column(String(100), nullable=False)
    node_type: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[Any | None] = mapped_column(JSONB)
    branches: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    wait_key: Mapped[str | None] = mapped_column(String(300))
    wait_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ExecutionEvent(Base):
    __tablename__ = "execution_events"
    __table_args__ = (
        Index("ix_exec_events_exec", "execution_id", "id"),
        Index("ix_exec_events_org_type_created", "org_id", "event_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("executions.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str | None] = mapped_column(String(100))
    scope: Mapped[str | None] = mapped_column(String(500))
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class Job(Base):
    """Durable work queue row. Claimed with FOR UPDATE SKIP LOCKED + lease."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_ready", "queue", "priority", "available_at", postgresql_where=text("status = 'queued'")),
        Index("ix_jobs_lease", "locked_until", postgresql_where=text("status = 'running'")),
        Index(
            "uq_jobs_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND status = 'queued'"),
        ),
        Index("ix_jobs_execution", "execution_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    queue: Mapped[str] = mapped_column(String(50), nullable=False, default="default")
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_key: Mapped[str | None] = mapped_column(String(300))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DeadLetter(IdMixin, Base):
    __tablename__ = "dead_letters"
    __table_args__ = (
        Index("ix_dead_letters_org", "org_id", "created_at"),
        Index("ix_dead_letters_execution", "execution_id"),
    )

    org_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    node_id: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(30), nullable=False)  # job | node
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    resolution: Mapped[str | None] = mapped_column(String(30))


# --------------------------------------------------------------------------- approvals


class ApprovalRequest(IdMixin, TimestampMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approvals_org_status", "org_id", "status", "due_at"),)

    org_id: Mapped[uuid.UUID] = _org_fk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("workspaces.id", ondelete="CASCADE"))
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("executions.id", ondelete="CASCADE"), index=True
    )
    node_run_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("node_runs.id", ondelete="CASCADE"))
    node_id: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="approval")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    form_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    approver_user_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUIDType), nullable=False, default=list)
    approver_team_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUIDType), nullable=False, default=list)
    approver_role: Mapped[str | None] = mapped_column(String(40))
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approvals_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column(Text)
    response_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ApprovalAction(IdMixin, Base):
    __tablename__ = "approval_actions"

    org_id: Mapped[uuid.UUID] = _org_fk()
    approval_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("approval_requests.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    actor_email: Mapped[str | None] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(30), nullable=False)  # approve|reject|comment|escalate|reassign|expire
    comment: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


# --------------------------------------------------------------------------- AI


class PromptTemplate(IdMixin, Base):
    __tablename__ = "prompt_templates"
    __table_args__ = (UniqueConstraint("org_id", "key", "version"),)

    org_id: Mapped[uuid.UUID] = _org_fk()
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AICall(IdMixin, Base):
    __tablename__ = "ai_calls"
    __table_args__ = (Index("ix_ai_calls_org_created", "org_id", "created_at"),)

    org_id: Mapped[uuid.UUID] = _org_fk()
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, index=True)
    node_id: Mapped[str | None] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(40), nullable=False, default="chat")
    prompt_key: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


# --------------------------------------------------------------------------- templates & files


class WorkflowTemplate(IdMixin, TimestampMixin, Base):
    __tablename__ = "workflow_templates"

    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(50)), nullable=False, default=list)
    required_connectors: Mapped[list[str]] = mapped_column(ARRAY(String(80)), nullable=False, default=list)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    install_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class StoredFile(IdMixin, Base):
    __tablename__ = "stored_files"

    org_id: Mapped[uuid.UUID] = _org_fk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUIDType, ForeignKey("workspaces.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str] = mapped_column(String(150), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="upload")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


__all__ = [
    "AUDIT_TSVECTOR_SQL",
    "AICall",
    "ApiKey",
    "ApprovalAction",
    "ApprovalRequest",
    "AuditLog",
    "Base",
    "Connection",
    "DeadLetter",
    "Execution",
    "ExecutionEvent",
    "Job",
    "NodeRun",
    "Organization",
    "PromptTemplate",
    "RefreshToken",
    "RoleAssignment",
    "Secret",
    "StoredFile",
    "Team",
    "TeamMember",
    "User",
    "Workflow",
    "WorkflowTemplate",
    "WorkflowTrigger",
    "WorkflowVersion",
    "Workspace",
    "uuid7",
]
