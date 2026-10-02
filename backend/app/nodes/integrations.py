"""Integration nodes.

* **Connector action nodes** are generated: every connector action becomes node type ``<connector>.<action>``
  whose config is ``connection_id`` + the action's input model.
* **Capability nodes** (``crm.upsert_contact``, ``comm.email``, ``comm.sms``, ``ticket.create`` ...) accept a
  connection of *any* connector implementing the capability — swap HubSpot for Pipedrive without editing nodes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.connectors.capabilities import CAPABILITIES
from app.connectors.registry import all_connectors, connectors_with_capability
from app.connectors.sdk import ActionSpec, Connector, ConnectorError
from app.nodes.base import Completed, NodeContext, NodeError, NodeResult, NodeType

_CATEGORY_MAP = {
    "communication": "communication",
    "crm": "business",
    "ticketing": "business",
    "storage": "business",
    "integration": "business",
    "data": "data",
}

_CAPABILITY_TYPES = {
    "email.send": ("comm.email", "communication", "Send email", "mail"),
    "sms.send": ("comm.sms", "communication", "Send SMS", "message"),
    "crm.find_contact": ("crm.find_contact", "business", "CRM: find contact", "user-search"),
    "crm.upsert_contact": ("crm.upsert_contact", "business", "CRM: create/update contact", "user-plus"),
    "crm.create_task": ("crm.create_task", "business", "CRM: create task", "list-todo"),
    "crm.add_note": ("crm.add_note", "business", "CRM: add note", "sticky-note"),
    "ticket.create": ("ticket.create", "business", "Ticket: create", "ticket"),
    "ticket.comment": ("ticket.comment", "business", "Ticket: comment", "message-square"),
}


def _with_connection(name: str, model: type[BaseModel], connector_hint: str) -> type[BaseModel]:
    fields: dict[str, Any] = {
        "connection_id": (
            str,
            Field(description="Connection to use", json_schema_extra={"x-connection": connector_hint}),
        ),
    }
    for fname, finfo in model.model_fields.items():
        fields[fname] = (finfo.annotation, finfo)
    return create_model(name, __config__=ConfigDict(extra="forbid"), **fields)  # type: ignore[call-overload]


async def _run(
    ctx: NodeContext, connection_id: str, connector_filter: set[str] | None, action_for: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    resolved = await ctx.runtime.connection(connection_id)
    connector: Connector = resolved.connector
    if connector_filter is not None and connector.key not in connector_filter:
        raise NodeError(f"Connection '{resolved.connection.name}' ({connector.key}) cannot be used by this node")
    action_key = action_for(connector)
    resolved.context.idempotency_key = ctx.idempotency_key
    resolved.context.timeout = ctx.timeout
    resolved.context.log = lambda level, msg, data=None: ctx.log(level, msg, data)
    try:
        return await connector.run_action(action_key, resolved.context, payload)
    except ConnectorError as exc:
        raise NodeError(
            exc.message,
            retryable=exc.retryable,
            details=exc.details,
            code=f"http_{exc.status_code}" if exc.status_code else None,
        ) from exc


def make_action_node(connector: Connector, spec: ActionSpec[Any, Any]) -> type[NodeType]:
    config_model = _with_connection(f"{connector.key}_{spec.key}_config", spec.input_model, connector.key)
    connector_key = connector.key

    class ActionNode(NodeType):
        type = f"{connector.key}.{spec.key}"
        category = _CATEGORY_MAP.get(connector.category, "business")
        label = f"{connector.name}: {spec.name}"
        description = spec.description
        icon = connector.icon
        output_schema = spec.output_model.model_json_schema()
        side_effects = not spec.idempotent

        async def execute(self, ctx: NodeContext, config: Any) -> NodeResult:
            data = config.model_dump(exclude={"connection_id"})
            out = await _run(ctx, config.connection_id, {connector_key}, lambda c: spec.key, data)
            return Completed(output=out)

    ActionNode.config_model = config_model
    ActionNode.connector_key = connector.key
    ActionNode.connector_action = spec.key
    ActionNode.__name__ = f"{connector.key.title().replace('_', '')}{spec.key.title().replace('_', '')}Node"
    return ActionNode


def make_capability_node(capability: str) -> type[NodeType]:
    in_model, out_model, _ = CAPABILITIES[capability]
    type_key, category, label, icon = _CAPABILITY_TYPES[capability]
    config_model = _with_connection(f"{type_key.replace('.', '_')}_config", in_model, f"capability:{capability}")

    class CapabilityNode(NodeType):
        async def execute(self, ctx: NodeContext, config: Any) -> NodeResult:
            providers = connectors_with_capability(capability)
            data = config.model_dump(exclude={"connection_id"})
            out = await _run(ctx, config.connection_id, set(providers), lambda c: providers[c.key], data)
            return Completed(output=out)

    CapabilityNode.type = type_key
    CapabilityNode.category = category
    CapabilityNode.label = label
    CapabilityNode.icon = icon
    CapabilityNode.description = f"{label} through any connection that supports it ({', '.join(sorted(connectors_with_capability(capability)))})."
    CapabilityNode.config_model = config_model
    CapabilityNode.output_schema = out_model.model_json_schema()
    CapabilityNode.capability = capability
    CapabilityNode.__name__ = f"{type_key.replace('.', '_').title()}Node"
    return CapabilityNode


class WebhookResponseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status_code: int = Field(default=200, ge=100, le=599)
    body: Any = None
    headers: dict[str, str] = Field(default_factory=dict)


class WebhookResponseNode(NodeType):
    type = "comm.webhook_response"
    category = "communication"
    label = "Webhook response"
    description = "Return an HTTP response to the caller of a 'wait_for_response' webhook trigger."
    icon = "reply"
    config_model = WebhookResponseConfig
    side_effects = False

    async def execute(self, ctx: NodeContext, config: WebhookResponseConfig) -> NodeResult:
        safe_headers = {
            k: v
            for k, v in config.headers.items()
            if k.lower() not in ("set-cookie", "content-length", "transfer-encoding", "host")
        }
        response = {"status_code": config.status_code, "body": config.body, "headers": safe_headers}
        await ctx.runtime.set_webhook_response(response)
        return Completed(output=response)


def integration_nodes() -> list[type[NodeType]]:
    nodes: list[type[NodeType]] = [WebhookResponseNode]
    for capability in _CAPABILITY_TYPES:
        nodes.append(make_capability_node(capability))
    for connector in all_connectors():
        for spec in connector.actions.values():
            nodes.append(make_action_node(connector, spec))
    return nodes
