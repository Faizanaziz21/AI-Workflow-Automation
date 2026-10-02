"""Static validation of workflow definitions (run on every save and enforced on publish)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import ValidationError

from app.engine.definition import WorkflowDefinition
from app.engine.expressions import ExpressionError, extract_expressions, is_template, parse, referenced_nodes
from app.engine.graph import LOOP_DONE_HANDLE, Graph
from app.nodes.registry import get_node_type, has_node_type


@dataclass
class Issue:
    level: Literal["error", "warning"]
    message: str
    node_id: str | None = None
    field: str | None = None
    edge_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ValidationReport:
    issues: list[Issue]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [i.as_dict() for i in self.errors],
            "warnings": [i.as_dict() for i in self.issues if i.level == "warning"],
        }


def _contains_template(value: Any) -> bool:
    if is_template(value):
        return True
    if isinstance(value, dict):
        return any(_contains_template(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_template(v) for v in value)
    return False


def _value_at(data: Any, loc: tuple[Any, ...]) -> Any:
    cur = data
    for part in loc:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and isinstance(part, int) and part < len(cur):
            cur = cur[part]
        else:
            return None
    return cur


def validate_definition(raw: dict[str, Any] | WorkflowDefinition) -> ValidationReport:
    issues: list[Issue] = []
    if isinstance(raw, WorkflowDefinition):
        definition = raw
    else:
        try:
            definition = WorkflowDefinition.model_validate(raw)
        except ValidationError as exc:
            return ValidationReport(
                [Issue("error", f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}") for e in exc.errors()]
            )

    ids = [n.id for n in definition.nodes]
    dupes = {i for i in ids if ids.count(i) > 1}
    for d in sorted(dupes):
        issues.append(Issue("error", f"Duplicate node id '{d}'", node_id=d))
    nodes = definition.node_map()

    # Node types + configs
    for node in definition.nodes:
        if not has_node_type(node.type):
            issues.append(Issue("error", f"Unknown node type '{node.type}'", node_id=node.id))
            continue
        nt = get_node_type(node.type)
        try:
            nt.config_model.model_validate(node.config)
        except ValidationError as exc:
            for err in exc.errors():
                loc = tuple(err["loc"])
                # Values that are expressions are only known at run time — validated after rendering.
                if _contains_template(_value_at(node.config, loc)):
                    continue
                if any(_contains_template(_value_at(node.config, loc[:i])) for i in range(1, len(loc))):
                    continue
                issues.append(Issue("error", err["msg"], node_id=node.id, field=".".join(str(p) for p in loc)))
        conn_field = nt.config_model.model_fields.get("connection_id")
        if conn_field is not None and conn_field.is_required() and not node.config.get("connection_id"):
            issues.append(Issue("error", "Select a connection", node_id=node.id, field="connection_id"))
        for msg in nt.semantic_errors(node.config, node):
            issues.append(Issue("error", msg, node_id=node.id))
        for expr in extract_expressions(node.config):
            try:
                parse(expr)
            except ExpressionError as exc:
                issues.append(
                    Issue("error", f"Expression error in '{{{{ {expr[:80]} }}}}': {exc.message}", node_id=node.id)
                )

    # Triggers
    triggers = [n for n in definition.nodes if n.type.startswith("trigger.") and not n.disabled]
    if len(triggers) == 0:
        issues.append(Issue("error", "Workflow needs exactly one trigger node"))
    elif len(triggers) > 1:
        for t in triggers[1:]:
            issues.append(Issue("error", "Only one trigger node is allowed", node_id=t.id))

    # Edges
    seen_edges: set[tuple[str, str, str]] = set()
    for e in definition.edges:
        eid = e.id or f"{e.source}->{e.target}"
        if e.source not in nodes or e.target not in nodes:
            issues.append(Issue("error", f"Edge references unknown node ({e.source} -> {e.target})", edge_id=eid))
            continue
        if e.source == e.target:
            issues.append(Issue("error", "Self-loop edges are not allowed", edge_id=eid, node_id=e.source))
        if nodes[e.target].type.startswith("trigger."):
            issues.append(Issue("error", "Trigger nodes cannot have incoming edges", edge_id=eid, node_id=e.target))
        key = (e.source, e.target, e.source_handle)
        if key in seen_edges:
            issues.append(Issue("warning", "Duplicate edge", edge_id=eid))
        seen_edges.add(key)
        if has_node_type(nodes[e.source].type):
            handles = get_node_type(nodes[e.source].type).handles(nodes[e.source].config)
            allowed = set(handles) | ({"error"} if nodes[e.source].on_error == "route" else set())
            if e.source_handle not in allowed:
                issues.append(
                    Issue(
                        "error",
                        f"Node '{e.source}' has no output handle '{e.source_handle}' "
                        f"(available: {', '.join(sorted(allowed)) or 'none'})",
                        edge_id=eid,
                        node_id=e.source,
                    )
                )

    if any(i.level == "error" for i in issues):
        return ValidationReport(issues)

    graph = Graph.build(definition)
    cycle = graph.find_cycle()
    if cycle:
        issues.append(
            Issue(
                "error", "Cycle detected: " + " -> ".join(cycle) + " (use a Loop node for iteration)", node_id=cycle[0]
            )
        )
        return ValidationReport(issues)

    # Loop structure: body nodes must stay inside the body; nothing outside may feed into the body.
    for loop_id, body in graph.loop_bodies.items():
        if not graph.body_entry_nodes(loop_id):
            issues.append(Issue("warning", "Loop has no 'body' branch", node_id=loop_id))
        for nid in body:
            for e in graph.incoming[nid]:
                if e.source != loop_id and e.source not in body:
                    issues.append(
                        Issue(
                            "error",
                            f"Node '{nid}' is inside loop '{loop_id}' but also receives input "
                            f"from '{e.source}' outside the loop",
                            node_id=nid,
                        )
                    )
                if e.source == loop_id and e.source_handle == LOOP_DONE_HANDLE:
                    issues.append(
                        Issue("error", "A node cannot be both in the loop body and after 'done'", node_id=nid)
                    )

    # Reachability
    if triggers:
        reachable = graph.reachable_from(triggers[0].id)
        for n in definition.nodes:
            if n.id not in reachable and not n.disabled:
                issues.append(Issue("warning", "Node is not reachable from the trigger", node_id=n.id))

    # Expression references must point at upstream nodes.
    for node in definition.nodes:
        if node.disabled:
            continue
        upstream = graph.ancestors(node.id)
        for expr in extract_expressions(node.config):
            try:
                refs = referenced_nodes(expr)
            except ExpressionError:
                continue
            for ref in refs:
                if ref not in nodes:
                    issues.append(Issue("error", f"Expression references unknown node '{ref}'", node_id=node.id))
                elif ref not in upstream:
                    issues.append(
                        Issue(
                            "warning",
                            f"Expression references '{ref}', which is not upstream; its "
                            "output may not exist when this node runs",
                            node_id=node.id,
                        )
                    )
    return ValidationReport(issues)


def connection_ids_in(definition: WorkflowDefinition) -> set[str]:
    """Collect connection ids referenced by node configs (static values only)."""
    out: set[str] = set()

    def walk(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif key == "connection_id" and isinstance(value, str) and value and not is_template(value):
            out.add(value)

    for n in definition.nodes:
        walk(n.config)
    return out
