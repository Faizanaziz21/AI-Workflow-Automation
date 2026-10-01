"""Helpers to build workflow definitions in tests."""

from __future__ import annotations

from typing import Any


def node(node_id: str, type_: str, config: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "type": type_, "name": extra.pop("name", node_id), "config": config or {}, **extra}


def edge(source: str, target: str, handle: str = "out") -> dict[str, Any]:
    return {"source": source, "target": target, "source_handle": handle}


def definition(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], **settings: Any) -> dict[str, Any]:
    return {"nodes": nodes, "edges": edges, "settings": settings}


def linear(
    *specs: tuple[str, str, dict[str, Any]],
    trigger: str = "trigger.manual",
    trigger_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    nodes = [node("start", trigger, trigger_config or {})]
    edges = []
    prev = "start"
    for node_id, type_, cfg in specs:
        nodes.append(node(node_id, type_, cfg))
        edges.append(edge(prev, node_id))
        prev = node_id
    return definition(nodes, edges)
