"""Workflow definition schema (the JSON document the visual editor produces)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NODE_ID_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=25, description="Total attempts including the first")
    initial_interval_seconds: float = Field(default=2.0, ge=0, le=3600)
    backoff_coefficient: float = Field(default=2.0, ge=1, le=10)
    max_interval_seconds: float = Field(default=300.0, ge=0, le=86400)
    jitter: bool = True
    retry_on: Literal["retryable", "all"] = Field(
        default="retryable", description="'retryable' retries only transient errors (timeouts, 429, 5xx)"
    )

    def delay_for(self, attempt: int, rand: float = 0.5) -> float:
        """Delay before attempt ``attempt + 1`` (``attempt`` is 1-based count of attempts made)."""
        base = self.initial_interval_seconds * (self.backoff_coefficient ** max(0, attempt - 1))
        base = min(base, self.max_interval_seconds)
        if self.jitter:
            base = base * (0.5 + rand)  # full-range jitter in [0.5x, 1.5x)
        return float(min(base, self.max_interval_seconds))


class Position(BaseModel):
    x: float = 0
    y: float = 0


class NodeDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=NODE_ID_PATTERN)
    type: str = Field(min_length=1, max_length=120)
    name: str = Field(default="", max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    position: Position = Field(default_factory=Position)
    retry: RetryPolicy | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=86400)
    on_error: Literal["fail", "continue", "route"] = Field(
        default="fail",
        description="fail: fail the execution; continue: emit error as output and continue; route: follow 'error' handle",
    )
    disabled: bool = False
    notes: str = Field(default="", max_length=5000)

    @property
    def label(self) -> str:
        return self.name or self.id


class EdgeDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    source: str
    target: str
    source_handle: str = "out"
    target_handle: str = "in"

    @field_validator("source_handle", "target_handle", mode="before")
    @classmethod
    def _default_handle(cls, v: Any) -> Any:
        return v or "out"


class WorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_timeout_seconds: int | None = Field(default=None, ge=1, le=60 * 60 * 24 * 90)
    max_parallel_nodes: int = Field(default=16, ge=1, le=256)
    default_retry: RetryPolicy = Field(default_factory=RetryPolicy)
    default_node_timeout_seconds: float | None = Field(default=None, gt=0, le=86400)
    mask_fields: list[str] = Field(default_factory=list, description="Extra output keys to mask in the inspector")


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    nodes: list[NodeDef] = Field(default_factory=list, max_length=500)
    edges: list[EdgeDef] = Field(default_factory=list, max_length=2000)
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)
    variables: dict[str, Any] = Field(default_factory=dict, description="Workflow constants available as vars.*")

    def node(self, node_id: str) -> NodeDef:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def node_map(self) -> dict[str, NodeDef]:
        return {n.id: n for n in self.nodes}

    def canonical_hash(self) -> str:
        return definition_hash(self.model_dump(mode="json"))


def definition_hash(definition: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
