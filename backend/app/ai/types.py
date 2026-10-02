"""Provider-neutral request/response types for the AI layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]


@dataclass
class ChatMessage:
    role: Role
    content: str


@dataclass
class ChatRequest:
    model: str
    messages: list[ChatMessage]
    system: str | None = None
    json_schema: dict[str, Any] | None = None
    schema_name: str = "output"
    temperature: float | None = None
    max_tokens: int = 4096
    effort: str | None = None  # low|medium|high (providers that support reasoning effort)
    timeout: float = 120.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    finish_reason: str | None = None
    provider_request_id: str | None = None
    served_by_fallback: bool = False


@dataclass
class EmbeddingResponse:
    vectors: list[list[float]]
    model: str
    input_tokens: int


class ProviderError(Exception):
    """Error from an LLM provider. ``retryable`` errors are retried and then fall back to the next model."""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None, refusal: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.refusal = refusal


def classify_status(status: int) -> bool:
    return status in (408, 409, 425, 429) or status >= 500
