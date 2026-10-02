"""Domain enumerations stored as strings in the database."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    SUPER_ADMIN = "super_admin"
    ORG_ADMIN = "org_admin"
    WORKFLOW_DEVELOPER = "workflow_developer"
    OPERATOR = "operator"
    APPROVER = "approver"
    VIEWER = "viewer"


class WorkflowStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class VersionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RETRYING = "RETRYING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_EXECUTION_STATUSES


TERMINAL_EXECUTION_STATUSES = frozenset({ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED})


class NodeRunStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


TERMINAL_NODE_STATUSES = frozenset(
    {NodeRunStatus.COMPLETED, NodeRunStatus.FAILED, NodeRunStatus.SKIPPED, NodeRunStatus.CANCELLED}
)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FAILED = "failed"
    DEAD = "dead"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class TriggerType(StrEnum):
    MANUAL = "manual"
    WEBHOOK = "webhook"
    SCHEDULE = "schedule"
    EMAIL = "email"
    API_EVENT = "api_event"
    DB_CHANGE = "db_change"
    FILE_UPLOADED = "file_uploaded"
    APP_EVENT = "app_event"
    REPLAY = "replay"
    SUB_WORKFLOW = "sub_workflow"


class ActorType(StrEnum):
    USER = "user"
    API_KEY = "api_key"
    SYSTEM = "system"
