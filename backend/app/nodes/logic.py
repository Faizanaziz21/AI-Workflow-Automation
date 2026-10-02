"""Control-flow nodes: IF, Switch, Loop, Parallel, Merge, Delay, Wait-until, Filter, Set variable, Stop, Sub-workflow."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.engine.definition import NodeDef
from app.engine.expressions import ExpressionError, evaluate, is_template, truthy
from app.nodes.base import Completed, NodeContext, NodeError, NodeResult, NodeType, Wait

Operator = Literal[
    "equals",
    "not_equals",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "is_empty",
    "is_not_empty",
    "in",
    "not_in",
    "matches",
    "is_true",
    "is_false",
]


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: Any = None
    operator: Operator = "equals"
    right: Any = None


def _num(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return v
    return v


def check_condition(c: Condition) -> bool:
    left, right, op = c.left, c.right, c.operator
    try:
        if op == "equals":
            return left == right or (_num(left) == _num(right) and not isinstance(_num(left), str))
        if op == "not_equals":
            return not check_condition(Condition(left=left, operator="equals", right=right))
        if op in ("gt", "gte", "lt", "lte"):
            a, b = _num(left), _num(right)
            if a is None or b is None:
                return False
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        if op == "contains":
            return right in left if left is not None else False
        if op == "not_contains":
            return right not in left if left is not None else True
        if op == "starts_with":
            return str(left or "").startswith(str(right))
        if op == "ends_with":
            return str(left or "").endswith(str(right))
        if op == "is_empty":
            return left in (None, "", [], {})
        if op == "is_not_empty":
            return left not in (None, "", [], {})
        if op == "in":
            return left in (right or [])
        if op == "not_in":
            return left not in (right or [])
        if op == "matches":
            return re.search(str(right), str(left or "")) is not None
        if op == "is_true":
            return truthy(left)
        if op == "is_false":
            return not truthy(left)
    except TypeError:
        return False
    return False


class IfConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition: Any = Field(default=None, description="Expression, e.g. {{ nodes.score.output.value >= 80 }}")
    conditions: list[Condition] = Field(default_factory=list, description="Structured conditions (alternative)")
    combinator: Literal["and", "or"] = "and"


class IfNode(NodeType):
    type = "logic.if"
    category = "logic"
    label = "IF / ELSE"
    description = "Route to the 'true' or 'false' branch."
    icon = "git-branch"
    config_model = IfConfig
    output_handles = ["true", "false"]
    side_effects = False
    output_schema = {"type": "object", "properties": {"result": {"type": "boolean"}}}

    def semantic_errors(self, config: dict[str, Any], node: NodeDef) -> list[str]:
        if config.get("condition") in (None, "") and not config.get("conditions"):
            return ["Provide a condition expression or at least one structured condition"]
        return []

    async def execute(self, ctx: NodeContext, config: IfConfig) -> NodeResult:
        results: list[bool] = []
        if config.condition not in (None, ""):
            results.append(truthy(config.condition))
        results.extend(check_condition(c) for c in config.conditions)
        result = all(results) if config.combinator == "and" else any(results)
        return Completed(output={"result": result}, branches=["true" if result else "false"])


class SwitchCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(pattern=r"^[A-Za-z0-9_-]{1,50}$")
    operator: Operator = "equals"
    value: Any = None


class SwitchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any = Field(default=None, description="Value to route on, e.g. {{ nodes.router.output.category }}")
    cases: list[SwitchCase] = Field(min_length=1, max_length=50)
    mode: Literal["first", "all"] = "first"

    @field_validator("cases")
    @classmethod
    def _unique(cls, v: list[SwitchCase]) -> list[SwitchCase]:
        handles = [c.handle for c in v]
        if len(set(handles)) != len(handles) or "default" in handles:
            raise ValueError("Case handles must be unique and not 'default'")
        return v


class SwitchNode(NodeType):
    type = "logic.switch"
    category = "logic"
    label = "Switch"
    description = "Route to the first (or every) matching case; 'default' when none match."
    icon = "split"
    config_model = SwitchConfig
    side_effects = False
    output_schema = {"type": "object", "properties": {"matched": {"type": "array"}, "value": {}}}

    def handles(self, config: dict[str, Any]) -> list[str]:
        return [c.get("handle") for c in config.get("cases", []) if isinstance(c, dict)] + ["default"]

    async def execute(self, ctx: NodeContext, config: SwitchConfig) -> NodeResult:
        matched = [
            c.handle
            for c in config.cases
            if check_condition(Condition(left=config.value, operator=c.operator, right=c.value))
        ]
        if config.mode == "first":
            matched = matched[:1]
        branches = matched or ["default"]
        return Completed(output={"matched": branches, "value": config.value}, branches=branches)


class LoopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: Any = Field(description="List to iterate, e.g. {{ nodes.fetch.output.rows }}")
    max_concurrency: int = Field(default=5, ge=1, le=50)
    max_items: int = Field(default=1000, ge=1, le=10000)


class LoopNode(NodeType):
    """Iterates its ``body`` sub-graph once per item (each iteration checkpointed in its own scope), then
    continues on ``done`` with the collected results."""

    type = "logic.loop"
    category = "logic"
    label = "Loop"
    description = "Run the 'body' branch for each item (bounded concurrency), then continue on 'done'."
    icon = "repeat"
    config_model = LoopConfig
    output_handles = ["body", "done"]
    side_effects = False
    output_schema = {"type": "object", "properties": {"count": {"type": "integer"}, "results": {"type": "array"}}}

    async def execute(self, ctx: NodeContext, config: LoopConfig) -> NodeResult:
        items = config.items
        if isinstance(items, dict):
            items = [{"key": k, "value": v} for k, v in items.items()]
        if not isinstance(items, list):
            raise NodeError(f"Loop items must be a list (got {type(items).__name__})")
        if len(items) > config.max_items:
            raise NodeError(f"Loop has {len(items)} items, exceeding max_items={config.max_items}")
        # The engine expands iterations from this state; the loop completes when all iterations finish.
        return Wait(state={"loop_items": items, "max_concurrency": config.max_concurrency}, reason="loop")


class ParallelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branches: int = Field(default=2, ge=2, le=20)


class ParallelNode(NodeType):
    type = "logic.parallel"
    category = "logic"
    label = "Parallel branch"
    description = "Fan out into N branches that run concurrently."
    icon = "columns"
    config_model = ParallelConfig
    side_effects = False

    def handles(self, config: dict[str, Any]) -> list[str]:
        return [f"branch_{i + 1}" for i in range(int(config.get("branches", 2) or 2))]

    async def execute(self, ctx: NodeContext, config: ParallelConfig) -> NodeResult:
        return Completed(
            output={"branches": config.branches}, branches=[f"branch_{i + 1}" for i in range(config.branches)]
        )


class MergeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["object", "list", "deep_merge"] = Field(
        default="object", description="object: {node_id: output}; list: [outputs]; deep_merge: merge dict outputs"
    )


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


class MergeNode(NodeType):
    type = "logic.merge"
    category = "logic"
    label = "Merge"
    description = "Wait for all active incoming branches and combine their outputs."
    icon = "merge"
    config_model = MergeConfig
    side_effects = False

    async def execute(self, ctx: NodeContext, config: MergeConfig) -> NodeResult:
        incoming: list[str] = ctx.data.get("incoming", [])
        outputs = {nid: (ctx.data["nodes"].get(nid) or {}).get("output") for nid in incoming}
        if config.mode == "list":
            return Completed(output={"items": list(outputs.values()), "sources": incoming})
        if config.mode == "deep_merge":
            merged: dict[str, Any] = {}
            for v in outputs.values():
                if isinstance(v, dict):
                    merged = _deep_merge(merged, v)
            return Completed(output=merged)
        return Completed(output=outputs)


class DelayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: float = Field(gt=0)
    unit: Literal["seconds", "minutes", "hours", "days"] = "minutes"

    def seconds(self) -> float:
        return self.amount * {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}[self.unit]


class DelayNode(NodeType):
    type = "logic.delay"
    category = "logic"
    label = "Delay"
    description = "Pause durably for a duration (survives restarts; consumes no worker while waiting)."
    icon = "timer"
    config_model = DelayConfig
    side_effects = False
    output_schema = {
        "type": "object",
        "properties": {"waited_seconds": {"type": "number"}, "resumed_at": {"type": "string"}},
    }

    async def execute(self, ctx: NodeContext, config: DelayConfig) -> NodeResult:
        if ctx.resume and ctx.resume.get("reason") == "timer":
            return Completed(output={"waited_seconds": config.seconds(), "resumed_at": ctx.now().isoformat()})
        return Wait(
            until=ctx.now() + timedelta(seconds=config.seconds()), reason=f"delay {config.amount} {config.unit}"
        )


class WaitUntilConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["datetime", "event"] = "event"
    until: str | None = Field(default=None, description="ISO datetime (datetime mode)")
    event_key: str | None = Field(
        default=None, description="Correlation key resumed via the Signals API, e.g. reply:{{ trigger.body.email }}"
    )
    timeout: float = Field(default=3, gt=0, description="Event mode timeout")
    timeout_unit: Literal["minutes", "hours", "days"] = "days"


class WaitUntilNode(NodeType):
    type = "logic.wait_until"
    category = "logic"
    label = "Wait until"
    description = "Wait for a point in time, or for an external event (with timeout branch)."
    icon = "hourglass"
    config_model = WaitUntilConfig
    side_effects = False
    output_schema = {"type": "object", "properties": {"received": {"type": "boolean"}, "payload": {}}}

    def handles(self, config: dict[str, Any]) -> list[str]:
        return ["received", "timeout"] if config.get("mode", "event") == "event" else ["out"]

    def semantic_errors(self, config: dict[str, Any], node: NodeDef) -> list[str]:
        mode = config.get("mode", "event")
        if mode == "event" and not config.get("event_key"):
            return ["event_key is required in event mode"]
        if mode == "datetime" and not config.get("until"):
            return ["until is required in datetime mode"]
        return []

    async def execute(self, ctx: NodeContext, config: WaitUntilConfig) -> NodeResult:
        from app.engine.expressions import _parse_dt

        resume = ctx.resume or {}
        if config.mode == "datetime":
            if resume.get("reason") == "timer":
                return Completed(output={"resumed_at": ctx.now().isoformat()})
            try:
                until = _parse_dt(config.until)
            except (ExpressionError, ValueError) as exc:
                raise NodeError(f"Invalid 'until' datetime: {config.until!r}") from exc
            if until <= ctx.now():
                return Completed(output={"resumed_at": ctx.now().isoformat(), "already_passed": True})
            return Wait(until=until, reason=f"until {until.isoformat()}")
        if resume.get("reason") == "signal":
            return Completed(
                output={"received": True, "payload": resume.get("payload"), "received_at": ctx.now().isoformat()},
                branches=["received"],
            )
        if resume.get("reason") == "timer":
            return Completed(output={"received": False, "timed_out_at": ctx.now().isoformat()}, branches=["timeout"])
        seconds = config.timeout * {"minutes": 60, "hours": 3600, "days": 86400}[config.timeout_unit]
        if not config.event_key:
            raise NodeError("event_key resolved to an empty value")
        return Wait(
            until=ctx.now() + timedelta(seconds=seconds),
            wait_key=str(config.event_key)[:300],
            reason=f"event '{config.event_key}'",
        )


class FilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: Any = Field(description="List to filter")
    condition: str = Field(
        description="Expression evaluated per element with `item` and `index`, e.g. {{ item.score > 50 }}"
    )
    fail_if_empty: bool = False


class FilterNode(NodeType):
    type = "logic.filter"
    category = "logic"
    label = "Filter"
    description = "Keep the list items for which the condition is true."
    icon = "filter"
    config_model = FilterConfig
    raw_fields = frozenset({"condition"})
    side_effects = False
    output_schema = {
        "type": "object",
        "properties": {"items": {"type": "array"}, "count": {"type": "integer"}, "removed": {"type": "integer"}},
    }

    async def execute(self, ctx: NodeContext, config: FilterConfig) -> NodeResult:
        items = config.items or []
        if not isinstance(items, list):
            raise NodeError("Filter items must be a list")
        expr = config.condition.strip()
        if is_template(expr):
            expr = expr.strip()[2:-2]
        kept = []
        for i, item in enumerate(items):
            try:
                if truthy(evaluate(expr, {**ctx.data, "item": item, "index": i})):
                    kept.append(item)
            except ExpressionError as exc:
                raise NodeError(f"Filter condition failed on item {i}: {exc.message}") from exc
        if config.fail_if_empty and not kept:
            raise NodeError("Filter produced no items")
        return Completed(output={"items": kept, "count": len(kept), "removed": len(items) - len(kept)})


class SetVariableConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignments: dict[str, Any] = Field(description="name -> value (expressions allowed); available as vars.<name>")

    @field_validator("assignments")
    @classmethod
    def _names(cls, v: dict[str, Any]) -> dict[str, Any]:
        for k in v:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k):
                raise ValueError(f"Invalid variable name '{k}'")
        return v


class SetVariableNode(NodeType):
    type = "logic.set_variable"
    category = "logic"
    label = "Set variables"
    description = "Assign workflow variables (vars.*) from expressions."
    icon = "variable"
    config_model = SetVariableConfig
    side_effects = False

    async def execute(self, ctx: NodeContext, config: SetVariableConfig) -> NodeResult:
        return Completed(output=config.assignments)


class StopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["success", "error"] = "error"
    message: str = "Stopped by workflow"


class StopNode(NodeType):
    type = "logic.stop"
    category = "logic"
    label = "Stop"
    description = "End the branch successfully or fail the execution with a message."
    icon = "octagon"
    config_model = StopConfig
    output_handles = []
    side_effects = False

    async def execute(self, ctx: NodeContext, config: StopConfig) -> NodeResult:
        if config.outcome == "error":
            raise NodeError(config.message, code="stopped")
        return Completed(output={"stopped": True, "message": config.message}, branches=[])


class SubWorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(description="Published workflow in the same workspace")
    input: dict[str, Any] = Field(default_factory=dict)
    wait_for_completion: bool = True


class SubWorkflowNode(NodeType):
    type = "logic.sub_workflow"
    category = "logic"
    label = "Execute workflow"
    description = "Start another published workflow (reusable sub-process) and optionally wait for its result."
    icon = "workflow"
    config_model = SubWorkflowConfig
    output_schema = {
        "type": "object",
        "properties": {"execution_id": {"type": "string"}, "status": {"type": "string"}, "output": {}},
    }

    async def execute(self, ctx: NodeContext, config: SubWorkflowConfig) -> NodeResult:
        resume = ctx.resume or {}
        if resume.get("reason") == "signal":
            payload = resume.get("payload") or {}
            if payload.get("status") != "COMPLETED":
                raise NodeError(
                    f"Sub-workflow {payload.get('execution_id')} ended with {payload.get('status')}",
                    details=payload.get("error"),
                )
            return Completed(output=payload)
        child_id = ctx.state.get("child_execution_id")
        if not child_id:
            child_id = await ctx.runtime.start_child_execution(config.workflow_id, config.input)
        if not config.wait_for_completion:
            return Completed(output={"execution_id": child_id, "status": "STARTED"})
        return Wait(
            wait_key=f"child:{child_id}", state={"child_execution_id": child_id}, reason=f"sub-workflow {child_id}"
        )


LOGIC_NODES: list[type[NodeType]] = [
    IfNode,
    SwitchNode,
    LoopNode,
    ParallelNode,
    MergeNode,
    DelayNode,
    WaitUntilNode,
    FilterNode,
    SetVariableNode,
    StopNode,
    SubWorkflowNode,
]
