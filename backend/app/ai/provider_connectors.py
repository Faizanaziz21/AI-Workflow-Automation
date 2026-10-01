"""AI providers exposed as connectors so their credentials use the same encrypted connection store,
access policies, test-connection and rotation flows as every other integration."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.ai.providers.anthropic import AnthropicProvider
from app.ai.providers.base import LLMProvider
from app.ai.providers.gemini import GeminiProvider
from app.ai.providers.openai import OpenAIProvider
from app.ai.types import ProviderError
from app.connectors.sdk import AuthSpec, AuthType, Connector, ConnectorContext, TestResult


class ApiKeyCredentials(BaseModel):
    api_key: str


class OptionalApiKeyCredentials(BaseModel):
    api_key: str | None = Field(default=None, description="Leave empty for servers without auth (e.g. local Ollama)")


class OpenAIConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    organization: str | None = None
    default_model: str = "gpt-4.1-mini"
    default_embedding_model: str = "text-embedding-3-small"
    supports_reasoning_effort: bool = False


class AnthropicConfig(BaseModel):
    base_url: str | None = Field(default=None, description="Override API base URL (proxies/gateways)")
    default_model: str = "claude-opus-5-5"
    server_side_fallback: bool = Field(
        default=True, description="Let Anthropic re-run classifier-declined requests on its recommended fallback model"
    )


class GeminiConfig(BaseModel):
    base_url: str = "https://generativelanguage.googleapis.com"
    default_model: str = "gemini-2.5-flash"
    default_embedding_model: str = "gemini-embedding-001"


class OpenAICompatibleConfig(BaseModel):
    base_url: str = Field(description="e.g. http://ollama:11434/v1 or http://vllm:8000/v1")
    default_model: str = "llama3.1:8b"
    default_embedding_model: str | None = "nomic-embed-text"
    json_mode: Literal["json_schema", "json_object", "none"] = Field(
        default="json_object", description="How the server supports structured output"
    )
    supports_reasoning_effort: bool = False


class AIProviderConnector(Connector):
    category = "ai"
    icon = "sparkles"
    provider: ClassVar[LLMProvider]

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            models = await self.provider.list_models(ctx)
        except ProviderError as exc:
            return TestResult(False, exc.message)
        default = getattr(ctx.config, "default_model", None)
        found = default in models if default else True
        msg = f"{len(models)} models available" + ("" if found else f"; default model '{default}' not listed")
        return TestResult(True, msg, {"models": models[:50]})


class OpenAIConnector(AIProviderConnector):
    key = "openai"
    name = "OpenAI"
    description = "GPT models and embeddings via the OpenAI API."
    auth = AuthSpec(AuthType.API_KEY, credentials_model=ApiKeyCredentials, config_model=OpenAIConfig)
    provider = OpenAIProvider()


class AnthropicConnector(AIProviderConnector):
    key = "anthropic"
    name = "Anthropic Claude"
    description = "Claude models via the Anthropic API (structured outputs, effort control, refusal fallback)."
    auth = AuthSpec(AuthType.API_KEY, credentials_model=ApiKeyCredentials, config_model=AnthropicConfig)
    provider = AnthropicProvider()


class GeminiConnector(AIProviderConnector):
    key = "gemini"
    name = "Google Gemini"
    description = "Gemini models and embeddings via the Generative Language API."
    auth = AuthSpec(AuthType.API_KEY, credentials_model=ApiKeyCredentials, config_model=GeminiConfig)
    provider = GeminiProvider()


class OpenAICompatibleConnector(AIProviderConnector):
    key = "openai_compatible"
    name = "Local / Open-source models"
    description = "Any OpenAI-compatible server: Ollama, vLLM, LM Studio, TGI, LiteLLM."
    auth = AuthSpec(AuthType.API_KEY, credentials_model=OptionalApiKeyCredentials, config_model=OpenAICompatibleConfig)
    provider = OpenAIProvider(json_mode="json_object", name="openai_compatible")


AI_PROVIDER_CONNECTORS: list[type[Connector]] = [
    OpenAIConnector,
    AnthropicConnector,
    GeminiConnector,
    OpenAICompatibleConnector,
]
