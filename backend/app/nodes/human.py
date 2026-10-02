"""Human-in-the-loop nodes: Approval, Manual review, Request information.

The node suspends the execution (``WAITING_APPROVAL``) after creating an approval request. Decisions arrive
through the Approvals API, which re-enters the node with ``resume.reason == "approval_decided"``; deadlines
re-enter it with ``reason == "timer"`` to escalate, auto-decide or fail. The execution resumes at exactly
this node — upstream outputs are already checkpointed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.definition import NodeDef
from app.nodes.base import Completed, NodeContext, NodeError, NodeResult, NodeType, Wait


class Approvers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_ids: list[str] = Field(default_factory=list)
    team_ids: list[str] = Field(default_factory=list)
    role: Literal["approver", "operator", "workflow_developer", "org_admin"] | None = Field(
        default=None, description="Anyone holding this role in the workspace may decide"
    )


class Escalation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    after_hours: float = Field(default=24, gt=0, le=24 * 90)
    escalate_to: Approvers = Field(default_factory=Approvers)
    max_escalations: int = Field(default=1, ge=0, le=5)


class _HumanBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=300)
    description: str = Field(default="", max_length=20_000)
    context: Any = Field(default=None, description="Data shown to the reviewer (expressions allowed)")
    approvers: Approvers = Field(default_factory=Approvers)
    due_in_hours: float = Field(default=48, gt=0, le=24 * 90)
    escalation: Escalation | None = None
    on_timeout: Literal["reject", "approve", "fail", "timeout_branch"] = "reject"


class ApprovalConfig(_HumanBase):
    required_approvals: int = Field(default=1, ge=1, le=10)


class ManualReviewConfig(_HumanBase):
    editable_fields: dict[str, Any] = Field(
        default_factory=dict, description="Initial values the reviewer may edit (returned as output.data)"
    )


class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    label: str = ""
    type: Literal["string", "text", "number", "boolean", "date", "select"] = "string"
    required: bool = True
    options: list[str] = Field(default_factory=list)


class RequestInfoConfig(_HumanBase):
    fields: list[FormField] = Field(min_length=1, max_length=50)


def _form_schema(fields: list[FormField]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for f in fields:
        t = {
            "string": "string",
            "text": "string",
            "number": "number",
            "boolean": "boolean",
            "date": "string",
            "select": "string",
        }[f.type]
        spec: dict[str, Any] = {"type": t, "title": f.label or f.name}
        if f.type == "select":
            spec["enum"] = f.options
        if f.type == "date":
            spec["format"] = "date"
        if f.type == "text":
            spec["x-multiline"] = True
        props[f.name] = spec
    return {"type": "object", "properties": props, "required": [f.name for f in fields if f.required]}


class _HumanNode(NodeType):
    category = "human"
    kind: str = "approval"
    side_effects = False
    default_timeout_seconds = None

    def handles(self, config: dict[str, Any]) -> list[str]:
        base = ["approved", "rejected"] if self.kind != "request_info" else ["out"]
        return base + (["timeout"] if config.get("on_timeout") == "timeout_branch" else [])

    def semantic_errors(self, config: dict[str, Any], node: NodeDef) -> list[str]:
        a = config.get("approvers") or {}
        if not (a.get("user_ids") or a.get("team_ids") or a.get("role")):
            return ["Select at least one approver (users, teams or a role)"]
        return []

    def _spec(self, ctx: NodeContext, config: _HumanBase) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": config.title,
            "description": config.description,
            "context": config.context,
            "approver_user_ids": config.approvers.user_ids,
            "approver_team_ids": config.approvers.team_ids,
            "approver_role": config.approvers.role,
            "due_at": (ctx.now() + timedelta(hours=config.due_in_hours)).isoformat(),
            "escalation": config.escalation.model_dump() if config.escalation else {},
        }

    def _decided(self, ctx: NodeContext, payload: dict[str, Any]) -> NodeResult:
        decision = payload.get("decision")
        output = {
            "decision": decision,
            "approved": decision == "approved",
            "comment": payload.get("comment"),
            "decided_by": payload.get("decided_by"),
            "decided_at": payload.get("decided_at"),
            "approval_id": ctx.state.get("approval_id"),
            "data": payload.get("response_data"),
            "escalation_level": payload.get("escalation_level", 0),
        }
        if self.kind == "request_info":
            return Completed(output=output, branches=["out"])
        return Completed(output=output, branches=["approved" if decision == "approved" else "rejected"])

    async def execute(self, ctx: NodeContext, config: Any) -> NodeResult:
        resume = ctx.resume or {}
        approval_id = ctx.state.get("approval_id")
        if resume.get("reason") == "approval_decided":
            return self._decided(ctx, resume.get("payload") or {})
        if approval_id and resume.get("reason") == "timer":
            approval = await ctx.runtime.get_approval(approval_id)
            if approval["status"] != "pending":
                return self._decided(ctx, approval)
            esc = config.escalation
            if (
                esc
                and approval["escalation_level"] < esc.max_escalations
                and (esc.escalate_to.user_ids or esc.escalate_to.team_ids or esc.escalate_to.role)
            ):
                updated = await ctx.runtime.escalate_approval(
                    approval_id,
                    {
                        "approver_user_ids": esc.escalate_to.user_ids,
                        "approver_team_ids": esc.escalate_to.team_ids,
                        "approver_role": esc.escalate_to.role,
                        "due_at": (ctx.now() + timedelta(hours=esc.after_hours)).isoformat(),
                    },
                )
                ctx.log("warning", f"Approval escalated to level {updated['escalation_level']}")
                return Wait(
                    until=ctx.now() + timedelta(hours=esc.after_hours),
                    state=ctx.state,
                    status="WAITING_APPROVAL",
                    reason="escalated approval",
                )
            outcome = config.on_timeout
            await ctx.runtime.expire_approval(approval_id, outcome)
            if outcome == "fail":
                raise NodeError("Approval deadline passed without a decision", code="approval_timeout")
            if outcome == "timeout_branch":
                return Completed(
                    output={"decision": "timeout", "approved": False, "approval_id": approval_id}, branches=["timeout"]
                )
            return self._decided(
                ctx,
                {
                    "decision": "approved" if outcome == "approve" else "rejected",
                    "comment": f"Auto-{outcome}d after deadline",
                    "decided_by": "system",
                },
            )
        if not approval_id:
            spec = self._spec(ctx, config)
            if isinstance(config, ApprovalConfig):
                spec["required_approvals"] = config.required_approvals
            if isinstance(config, ManualReviewConfig):
                spec["response_template"] = config.editable_fields
            if isinstance(config, RequestInfoConfig):
                spec["form_schema"] = _form_schema(config.fields)
            approval_id = await ctx.runtime.create_approval(spec)
        first_deadline = config.escalation.after_hours if config.escalation else config.due_in_hours
        return Wait(
            until=ctx.now() + timedelta(hours=min(first_deadline, config.due_in_hours)),
            state={"approval_id": approval_id},
            status="WAITING_APPROVAL",
            reason=config.title,
        )


class ApprovalNode(_HumanNode):
    type = "human.approval"
    label = "Approval"
    description = "Suspend until approver(s) approve or reject; supports deadlines, escalation and quorum."
    icon = "check-circle"
    kind = "approval"
    config_model = ApprovalConfig
    output_schema = {
        "type": "object",
        "properties": {
            "decision": {"type": "string"},
            "approved": {"type": "boolean"},
            "comment": {"type": "string"},
            "decided_by": {"type": "string"},
        },
    }


class ManualReviewNode(_HumanNode):
    type = "human.manual_review"
    label = "Manual review"
    description = "A reviewer inspects (and may edit) data, then approves or rejects."
    icon = "eye"
    kind = "manual_review"
    config_model = ManualReviewConfig
    output_schema = {"type": "object", "properties": {"decision": {"type": "string"}, "data": {"type": "object"}}}


class RequestInfoNode(_HumanNode):
    type = "human.request_info"
    label = "Request information"
    description = "Ask a person to fill in a form; the submitted data becomes the node output."
    icon = "clipboard"
    kind = "request_info"
    config_model = RequestInfoConfig
    output_schema = {"type": "object", "properties": {"data": {"type": "object"}}}


HUMAN_NODES: list[type[NodeType]] = [ApprovalNode, ManualReviewNode, RequestInfoNode]
