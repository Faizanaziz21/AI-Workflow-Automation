"""Node type registry (built-in nodes, generated connector nodes, plugin nodes)."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from app.nodes.base import NodeType

logger = logging.getLogger(__name__)
_nodes: dict[str, NodeType] = {}
_loaded = False


def register(node_cls: type[NodeType]) -> None:
    if node_cls.type in _nodes:
        raise ValueError(f"Duplicate node type {node_cls.type}")
    _nodes[node_cls.type] = node_cls()


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.nodes.ai import AI_NODES
    from app.nodes.data import DATA_NODES
    from app.nodes.human import HUMAN_NODES
    from app.nodes.integrations import integration_nodes
    from app.nodes.logic import LOGIC_NODES
    from app.nodes.triggers import TRIGGER_NODES

    for cls in [*TRIGGER_NODES, *LOGIC_NODES, *DATA_NODES, *AI_NODES, *HUMAN_NODES, *integration_nodes()]:
        register(cls)
    for ep in entry_points(group="flowforge.nodes"):
        try:
            register(ep.load())
        except Exception:
            logger.exception("Failed to load node plugin %s", ep.name)


def get_node_type(type_key: str) -> NodeType:
    _load()
    try:
        return _nodes[type_key]
    except KeyError as exc:
        raise KeyError(f"Unknown node type '{type_key}'") from exc


def has_node_type(type_key: str) -> bool:
    _load()
    return type_key in _nodes


def all_node_types() -> list[NodeType]:
    _load()
    order = {"trigger": 0, "logic": 1, "data": 2, "ai": 3, "communication": 4, "business": 5, "human": 6}
    return sorted(_nodes.values(), key=lambda n: (order.get(n.category, 9), n.label))
