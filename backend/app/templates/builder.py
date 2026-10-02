"""Tiny DSL for authoring workflow templates with automatic left-to-right layout.

Connection placeholders (``conn("crm")`` -> ``"${connection:crm}"``) are substituted at install time with
connection ids chosen by the installer, so templates never embed credentials or tenant ids.
"""

from __future__ import annotations

import itertools
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

PLACEHOLDER = "${connection:%s}"


def conn(role: str) -> str:
    return PLACEHOLDER % role


@dataclass
class TemplateSpec:
    slug: str
    name: str
    category: str
    summary: str
    description: str
    tags: list[str]
    connection_roles: dict[str, str]  # role -> connector key or "capability:x" / "category:ai"
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    featured: bool = False

    def node(self, node_id: str, type_: str, name: str, config: dict[str, Any] | None = None, **extra: Any) -> str:
        self.nodes.append({"id": node_id, "type": type_, "name": name, "config": config or {}, **extra})
        return node_id

    def edge(self, source: str, target: str, handle: str = "out") -> None:
        self.edges.append({"source": source, "target": target, "source_handle": handle})

    def chain(self, *ids: str) -> None:
        for a, b in itertools.pairwise(ids):
            self.edge(a, b)

    def definition(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "nodes": layout(self.nodes, self.edges),
            "edges": self.edges,
            "settings": self.settings,
            "variables": self.variables,
        }


def layout(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], dx: int = 300, dy: int = 140
) -> list[dict[str, Any]]:
    """Longest-path layering: x by depth from the trigger, y spread within each layer."""
    incoming: dict[str, int] = defaultdict(int)
    out: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        incoming[e["target"]] += 1
        out[e["source"]].append(e["target"])
    depth = {n["id"]: 0 for n in nodes}
    queue = deque(n["id"] for n in nodes if incoming[n["id"]] == 0)
    remaining = dict(incoming)
    while queue:
        nid = queue.popleft()
        for t in out[nid]:
            depth[t] = max(depth[t], depth[nid] + 1)
            remaining[t] -= 1
            if remaining[t] == 0:
                queue.append(t)
    layers: dict[int, list[str]] = defaultdict(list)
    for n in nodes:
        layers[depth[n["id"]]].append(n["id"])
    pos: dict[str, dict[str, float]] = {}
    for d, ids in layers.items():
        offset = (len(ids) - 1) * dy / 2
        for i, nid in enumerate(ids):
            pos[nid] = {"x": 80 + d * dx, "y": 320 + i * dy - offset}
    return [{**n, "position": pos[n["id"]]} for n in nodes]


def substitute(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("${connection:") and value.endswith("}"):
        return mapping.get(value[len("${connection:") : -1], "")
    if isinstance(value, dict):
        return {k: substitute(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, mapping) for v in value]
    return value


def placeholders(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str) and value.startswith("${connection:"):
        found.add(value[len("${connection:") : -1])
    elif isinstance(value, dict):
        for v in value.values():
            found |= placeholders(v)
    elif isinstance(value, list):
        for v in value:
            found |= placeholders(v)
    return found
