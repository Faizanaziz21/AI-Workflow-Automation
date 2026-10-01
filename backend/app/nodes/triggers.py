"""Trigger nodes. A workflow has exactly one; its output is the (normalised) trigger payload."""

from __future__ import annotations

from typing import Any, Literal

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field

from app.engine.definition import NodeDef
from app.nodes.base import Completed, NodeContext, NodeResult, NodeType


class _TriggerBase(NodeType):
    category = "trigger"
    is_trigger = True
    side_effects = False

    async def execute(self, ctx: NodeContext, config: Any) -> NodeResult:
        return Completed(output=ctx.data.get("trigger", {}))


class ManualConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_schema: dict[str, Any] | None = Field(default=None, description="JSON schema for the run form")
    sample_payload: dict[str, Any] = Field(default_factory=dict)


class ManualTrigger(_TriggerBase):
    type = "trigger.manual"
    label = "Manual trigger"
    description = "Start the workflow from the console or API with an optional input form."
    icon = "play"
    trigger_type = "manual"
    config_model = ManualConfig
    output_schema = {"type": "object", "description": "The submitted input payload"}


class WebhookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    methods: list[Literal["POST", "PUT", "GET"]] = ["POST"]
    authentication: Literal["signature", "api_key", "none"] = Field(
        default="signature", description="signature: HMAC-SHA256 (X-FlowForge-Signature); api_key: X-API-Key header"
    )
    response_mode: Literal["immediate", "wait_for_response"] = Field(
        default="immediate", description="wait_for_response returns the payload of a 'Webhook response' node"
    )
    idempotency_header: str = Field(default="Idempotency-Key", description="Header used to dedupe deliveries")
    correlation_key: str | None = Field(default=None, description="Expression over the payload, e.g. {{ body.email }}")
    sample_payload: dict[str, Any] = Field(default_factory=dict)


class WebhookTrigger(_TriggerBase):
    type = "trigger.webhook"
    label = "Webhook"
    description = "Start on an HTTP request to a unique, signed URL."
    icon = "webhook"
    trigger_type = "webhook"
    config_model = WebhookConfig
    output_schema = {
        "type": "object",
        "properties": {
            "body": {},
            "headers": {"type": "object"},
            "query": {"type": "object"},
            "method": {"type": "string"},
            "received_at": {"type": "string"},
        },
    }


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cron: str = Field(description="Cron expression, e.g. '0 9 * * 1-5'")
    timezone: str = "UTC"
    payload: dict[str, Any] = Field(default_factory=dict, description="Static payload passed to each run")


class ScheduleTrigger(_TriggerBase):
    type = "trigger.schedule"
    label = "Schedule"
    description = "Run on a cron schedule (exactly-once per tick, even with many schedulers)."
    icon = "clock"
    trigger_type = "schedule"
    config_model = ScheduleConfig
    output_schema = {
        "type": "object",
        "properties": {"scheduled_for": {"type": "string"}, "payload": {"type": "object"}},
    }

    def semantic_errors(self, config: dict[str, Any], node: NodeDef) -> list[str]:
        cron = config.get("cron", "")
        errors = []
        if not croniter.is_valid(cron):
            errors.append(f"Invalid cron expression '{cron}'")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(config.get("timezone", "UTC"))
        except Exception:
            errors.append(f"Unknown timezone '{config.get('timezone')}'")
        return errors


class PollingConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    connection_id: str = Field(json_schema_extra={"x-connection": True})
    poll_interval_seconds: int = Field(default=60, ge=15, le=86400)


class EmailTriggerConfig(PollingConfig):
    folder: str = "INBOX"
    from_filter: str | None = None
    subject_filter: str | None = None
    mark_seen: bool = True


class EmailTrigger(_TriggerBase):
    type = "trigger.email"
    label = "Email received"
    description = "Start for each new email in an IMAP mailbox."
    icon = "mail"
    trigger_type = "email"
    connector_key = "email"
    config_model = EmailTriggerConfig
    output_schema = {
        "type": "object",
        "properties": {
            "from_address": {"type": "string"},
            "from_name": {"type": "string"},
            "subject": {"type": "string"},
            "text": {"type": "string"},
            "html": {"type": "string"},
            "message_id": {"type": "string"},
        },
    }


class ApiEventConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_name: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,200}$", description="e.g. order.created")
    filter: str | None = Field(default=None, description="Optional expression over {{ event }}; run only when truthy")


class ApiEventTrigger(_TriggerBase):
    type = "trigger.api_event"
    label = "API event"
    description = "Start when an event with this name is published to POST /api/v1/events."
    icon = "zap"
    trigger_type = "api_event"
    config_model = ApiEventConfig
    output_schema = {
        "type": "object",
        "properties": {"event": {"type": "string"}, "data": {}, "event_id": {"type": "string"}},
    }


class DbChangeConfig(PollingConfig):
    table: str
    cursor_column: str
    batch_size: int = Field(default=100, ge=1, le=1000)
    mode: Literal["per_row", "batch"] = Field(default="per_row", description="One execution per row or per batch")


class DbChangeTrigger(_TriggerBase):
    type = "trigger.db_change"
    label = "Database change"
    description = "Start when rows are inserted/updated in a PostgreSQL or MySQL table (cursor-based CDC polling)."
    icon = "database"
    trigger_type = "db_change"
    config_model = DbChangeConfig
    output_schema = {"type": "object", "properties": {"row": {"type": "object"}, "rows": {"type": "array"}}}


class FileUploadedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["platform", "google_drive"] = "platform"
    connection_id: str | None = Field(default=None, json_schema_extra={"x-connection": True})
    folder_id: str | None = None
    filename_pattern: str = Field(default="*", description="Glob, e.g. *.pdf")
    poll_interval_seconds: int = Field(default=60, ge=15, le=86400)


class FileUploadedTrigger(_TriggerBase):
    type = "trigger.file_uploaded"
    label = "File uploaded"
    description = "Start when a file is uploaded to FlowForge or appears in a Google Drive folder."
    icon = "file-up"
    trigger_type = "file_uploaded"
    config_model = FileUploadedConfig
    output_schema = {"type": "object", "properties": {"file": {"type": "object"}, "source": {"type": "string"}}}

    def semantic_errors(self, config: dict[str, Any], node: NodeDef) -> list[str]:
        if config.get("source") == "google_drive" and (not config.get("connection_id") or not config.get("folder_id")):
            return ["Google Drive source requires connection_id and folder_id"]
        return []


TRIGGER_NODES: list[type[NodeType]] = [
    ManualTrigger,
    WebhookTrigger,
    ScheduleTrigger,
    EmailTrigger,
    ApiEventTrigger,
    DbChangeTrigger,
    FileUploadedTrigger,
]
