from __future__ import annotations

from typing import Any, Protocol

from app.ai.types import ChatRequest, ChatResponse, EmbeddingResponse
from app.connectors.sdk import ConnectorContext


class LLMProvider(Protocol):
    """Adapter contract every model provider implements."""

    name: str

    async def chat(self, ctx: ConnectorContext, request: ChatRequest) -> ChatResponse: ...

    async def embed(self, ctx: ConnectorContext, texts: list[str], model: str) -> EmbeddingResponse: ...

    async def list_models(self, ctx: ConnectorContext) -> list[str]: ...


def strip_code_fences(text: str) -> str:
    """Models occasionally wrap JSON in ```json fences even when asked not to."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def schema_instruction(schema: dict[str, Any]) -> str:
    import json

    return (
        "Respond ONLY with a single JSON value that validates against this JSON Schema. "
        "Do not include markdown fences or commentary.\n" + json.dumps(schema, separators=(",", ":"))
    )
