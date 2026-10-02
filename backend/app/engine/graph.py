"""Graph analysis over a workflow definition: adjacency, cycles, loop scopes and reachability.

Scopes: a Loop node's ``body`` handle starts a sub-graph executed once per item. Node runs inside an
iteration carry a scope string such as ``loop1:3`` (nested: ``loop1:3/loop2:0``). Nodes outside any loop
have the empty scope.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from app.engine.definition import EdgeDef, WorkflowDefinition

LOOP_TYPE = "logic.loop"
LOOP_BODY_HANDLE = "body"
LOOP_DONE_HANDLE = "done"


@dataclass
class Graph:
    definition: WorkflowDefinition
    outgoing: dict[str, list[EdgeDef]] = field(default_factory=lambda: defaultdict(list))
    incoming: dict[str, list[EdgeDef]] = field(default_factory=lambda: defaultdict(list))
    # loop node id -> set of body node ids (transitively, includes nested loop bodies)
    loop_bodies: dict[str, set[str]] = field(default_factory=dict)
    # node id -> innermost enclosing loop id (None for top level)
    parent_loop: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def build(cls, definition: WorkflowDefinition) -> Graph:
        g = cls(definition)
        ids = {n.id for n in definition.nodes if not n.disabled}
        for e in definition.edges:
            if e.source in ids and e.target in ids:
                g.outgoing[e.source].append(e)
                g.incoming[e.target].append(e)
        g._compute_loops()
        return g

    @property
    def node_ids(self) -> list[str]:
        return [n.id for n in self.definition.nodes if not n.disabled]

    def trigger_ids(self) -> list[str]:
        return [n.id for n in self.definition.nodes if n.type.startswith("trigger.") and not n.disabled]

    def _compute_loops(self) -> None:
        loops = [n.id for n in self.definition.nodes if n.type == LOOP_TYPE and not n.disabled]
        for loop_id in loops:
            body: set[str] = set()
            queue = deque(e.target for e in self.outgoing[loop_id] if e.source_handle == LOOP_BODY_HANDLE)
            while queue:
                nid = queue.popleft()
                if nid in body or nid == loop_id:
                    continue
                body.add(nid)
                for e in self.outgoing[nid]:
                    queue.append(e.target)
            self.loop_bodies[loop_id] = body
        for nid in self.node_ids:
            enclosing = [lid for lid, body in self.loop_bodies.items() if nid in body]
            # innermost = the enclosing loop whose body is smallest
            self.parent_loop[nid] = min(enclosing, key=lambda lid: len(self.loop_bodies[lid])) if enclosing else None

    def body_entry_nodes(self, loop_id: str) -> list[str]:
        return [e.target for e in self.outgoing[loop_id] if e.source_handle == LOOP_BODY_HANDLE]

    def direct_body(self, loop_id: str) -> set[str]:
        """Body nodes whose innermost loop is ``loop_id``."""
        return {n for n in self.loop_bodies.get(loop_id, set()) if self.parent_loop.get(n) == loop_id}

    def body_terminals(self, loop_id: str) -> list[str]:
        body = self.direct_body(loop_id)
        return [n for n in body if not any(e.target in body for e in self.outgoing[n])]

    def find_cycle(self) -> list[str] | None:
        """Return a cycle path if the graph (ignoring nothing) has one."""
        color: dict[str, int] = {}
        stack: list[str] = []

        def visit(n: str) -> list[str] | None:
            color[n] = 1
            stack.append(n)
            for e in self.outgoing[n]:
                if color.get(e.target) == 1:
                    return [*stack[stack.index(e.target) :], e.target]
                if color.get(e.target, 0) == 0:
                    found = visit(e.target)
                    if found:
                        return found
            stack.pop()
            color[n] = 2
            return None

        for n in self.node_ids:
            if color.get(n, 0) == 0:
                found = visit(n)
                if found:
                    return found
        return None

    def reachable_from(self, start: str) -> set[str]:
        seen: set[str] = set()
        queue = deque([start])
        while queue:
            n = queue.popleft()
            if n in seen:
                continue
            seen.add(n)
            queue.extend(e.target for e in self.outgoing[n])
        return seen

    def ancestors(self, node_id: str) -> set[str]:
        seen: set[str] = set()
        queue = deque(e.source for e in self.incoming[node_id])
        while queue:
            n = queue.popleft()
            if n in seen:
                continue
            seen.add(n)
            queue.extend(e.source for e in self.incoming[n])
        return seen

    def descendants(self, node_id: str) -> set[str]:
        return self.reachable_from(node_id) - {node_id}


def scope_for(parent_scope: str, loop_id: str, index: int) -> str:
    part = f"{loop_id}:{index}"
    return f"{parent_scope}/{part}" if parent_scope else part


def parent_scope(scope: str) -> str:
    return scope.rsplit("/", 1)[0] if "/" in scope else ""


def scope_chain(scope: str) -> list[str]:
    """``a:1/b:2`` -> ``["a:1/b:2", "a:1", ""]`` (innermost first)."""
    chain = [scope]
    while scope:
        scope = parent_scope(scope)
        chain.append(scope)
    return chain


def scope_index(scope: str) -> int | None:
    if not scope:
        return None
    return int(scope.rsplit("/", 1)[-1].split(":")[1])
