"""Node type contract.

Every node type declares:

* ``config_model`` — Pydantic model; its JSON schema drives the editor's configuration form and validation.
* ``output_schema`` — JSON schema of the node output (shown in the editor's data picker / inspector).
* ``handles(config)`` — output handles (``out`` by default; IF has ``true``/``false``; Switch is dynamic).
* ``execute(ctx, config)`` — returns :class:`Completed` or :class:`Wait`, or raises :class:`NodeError`.

Waiting nodes (delay, wait-until, approvals) are *re-entered* when their timer fires or a signal/decision
arrives: ``ctx.resume`` then carries the reason and payload, and ``ctx.state`` holds whatever the node
saved on the previous entry. Nothing is kept in memory between entries, so waits survive restarts.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.engine.definition import NodeDef, RetryPolicy

if TYPE_CHECKING:
    from app.ai.gateway import AIGateway
    from app.connectors.sdk import FileStore
    from app.services.connections import ResolvedConnection


class EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Completed:
    output: Any
    branches: list[str] | None = None  # None => ["out"]


@dataclass
class Wait:
    until: datetime | None = None
    wait_key: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    status: Literal["WAITING", "WAITING_APPROVAL"] = "WAITING"
    reason: str = ""


NodeResult = Completed | Wait


class NodeError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, details: Any = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.details = details
        self.code = code


class NodeRuntime(Protocol):
    """Platform services available to node handlers (implemented by the engine; faked in unit tests)."""

    async def connection(self, connection_id: str) -> ResolvedConnection: ...

    def ai(self) -> AIGateway: ...

    @property
    def files(self) -> FileStore: ...

    async def create_approval(self, spec: dict[str, Any]) -> str: ...

    async def get_approval(self, approval_id: str) -> dict[str, Any]: ...

    async def escalate_approval(self, approval_id: str, spec: dict[str, Any]) -> dict[str, Any]: ...

    async def expire_approval(self, approval_id: str, outcome: str) -> None: ...

    async def set_webhook_response(self, response: dict[str, Any]) -> None: ...

    async def start_child_execution(self, workflow_id: str, payload: dict[str, Any]) -> str: ...

    def register_secrets(self, values: list[str]) -> None: ...


@dataclass
class NodeContext:
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    workflow_id: uuid.UUID
    execution_id: uuid.UUID
    node: NodeDef
    scope: str
    attempt: int
    idempotency_key: str
    data: dict[str, Any]
    runtime: NodeRuntime
    resume: dict[str, Any] | None = None
    state: dict[str, Any] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    timeout: float = 300.0
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def log(self, level: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.logs.append({"level": level, "message": message[:2000], "data": data or {}, "ts": self.now().isoformat()})


class NodeType:
    type: ClassVar[str]
    category: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str] = ""
    icon: ClassVar[str] = "box"
    config_model: ClassVar[type[BaseModel]] = EmptyConfig
    output_schema: ClassVar[dict[str, Any] | None] = None
    output_handles: ClassVar[list[str]] = ["out"]
    # Config fields NOT rendered before execute() (evaluated by the node itself, e.g. per item).
    raw_fields: ClassVar[frozenset[str]] = frozenset()
    is_trigger: ClassVar[bool] = False
    trigger_type: ClassVar[str | None] = None
    default_timeout_seconds: ClassVar[float | None] = None
    default_retry: ClassVar[RetryPolicy | None] = None
    connector_key: ClassVar[str | None] = None
    connector_action: ClassVar[str | None] = None
    capability: ClassVar[str | None] = None
    side_effects: ClassVar[bool] = True

    def handles(self, config: dict[str, Any]) -> list[str]:
        return list(self.output_handles)

    def semantic_errors(self, config: dict[str, Any], node: NodeDef) -> list[str]:
        """Extra validation beyond the config schema (static checks only)."""
        return []

    async def execute(self, ctx: NodeContext, config: Any) -> NodeResult:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "category": self.category,
            "label": self.label,
            "description": self.description,
            "icon": self.icon,
            "config_schema": self.config_model.model_json_schema(),
            "output_schema": self.output_schema,
            "handles": list(self.output_handles),
            "dynamic_handles": type(self).handles is not NodeType.handles,
            "is_trigger": self.is_trigger,
            "trigger_type": self.trigger_type,
            "connector_key": self.connector_key,
            "capability": self.capability,
            "raw_fields": sorted(self.raw_fields),
            "default_timeout_seconds": self.default_timeout_seconds,
            "default_retry": self.default_retry.model_dump() if self.default_retry else None,
        }


def schema_of(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()
