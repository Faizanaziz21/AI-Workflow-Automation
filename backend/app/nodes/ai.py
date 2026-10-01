"""AI nodes. All model access goes through the AI gateway (routing, schema enforcement, fallback, cost)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ai.gateway import AIError, AIRequest, AIResult, ModelRef
from app.ai.prompts import (
    BUILTIN_PROMPTS,
    CONFIDENCE,
    DOCUMENT_SCHEMA,
    EMAIL_ROUTER_SCHEMA,
    LEAD_SCHEMA,
    REASONING,
    SENTIMENT_SCHEMA,
    SUMMARY_SCHEMA,
    resolve_prompt,
)
from app.engine.expressions import ExpressionError, render, stringify
from app.nodes.base import Completed, NodeContext, NodeError, NodeResult, NodeType
from app.nodes.data import extract_text

USAGE_SCHEMA = {"type": "object", "description": "provider, model, tokens, cost_usd, latency_ms, attempts"}


class ModelChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str | None = Field(default=None, json_schema_extra={"x-connection": "category:ai"})
    model: str | None = None


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str | None = Field(
        default=None,
        description="AI provider connection (defaults to the workspace's first AI connection)",
        json_schema_extra={"x-connection": "category:ai"},
    )
    model: str | None = Field(default=None, description="Model id (defaults to the connection's default model)")
    fallbacks: list[ModelChoice] = Field(default_factory=list, max_length=5, description="Tried in order on failure")
    temperature: float | None = Field(
        default=None, ge=0, le=2, description="Ignored by models without sampling controls"
    )
    max_tokens: int = Field(default=2048, ge=16, le=64000)
    effort: Literal["low", "medium", "high"] | None = None
    prompt_version: int | None = Field(default=None, ge=0, description="Pin a prompt version (0 = built-in)")
    max_repair_attempts: int = Field(default=2, ge=0, le=5)

    def refs(self) -> list[ModelRef]:
        return [ModelRef(self.connection_id, self.model)] + [ModelRef(f.connection_id, f.model) for f in self.fallbacks]


async def run_prompt(
    ctx: NodeContext,
    key: str,
    settings: ModelSettings,
    inputs: dict[str, Any],
    *,
    output_schema: dict[str, Any] | None = None,
    use_library_schema: bool = True,
    system_override: str | None = None,
    user_override: str | None = None,
    operation: str = "chat",
) -> AIResult:
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        prompt = await resolve_prompt(session, ctx.org_id, key, settings.prompt_version)
    scope = {**ctx.data, "input": inputs}
    try:
        system = render(system_override if system_override is not None else prompt.system, scope)
        user = render(user_override if user_override is not None else prompt.user, scope)
    except ExpressionError as exc:
        raise NodeError(f"Prompt template error: {exc.message}") from exc
    schema = output_schema
    if schema is None and use_library_schema:
        builtin = BUILTIN_PROMPTS.get(key)
        # Custom prompt versions may omit a schema; the node's output contract still applies.
        schema = prompt.output_schema or (builtin.output_schema if builtin else None)
    request = AIRequest(
        prompt=stringify(user),
        system=stringify(system) or None,
        output_schema=schema,
        models=settings.refs(),
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        effort=settings.effort,
        max_repair_attempts=settings.max_repair_attempts,
        timeout=min(ctx.timeout, 300),
        prompt_key=prompt.key,
        prompt_version=prompt.version,
        operation=operation,
    )
    try:
        result = await ctx.runtime.ai().complete(request)
    except AIError as exc:
        raise NodeError(
            f"AI call failed: {exc.message}", retryable=exc.retryable, details={"attempts": exc.attempts}
        ) from exc
    ctx.log(
        "info",
        f"AI {result.provider}/{result.model}: {result.input_tokens}+{result.output_tokens} tokens, "
        f"${result.cost_usd:.5f}",
        {"attempts": result.attempts, "fallback_used": result.fallback_used},
    )
    return result


class _AINode(NodeType):
    category = "ai"
    icon = "sparkles"
    side_effects = False


# ----------------------------------------------------------------------------- generic


class PromptConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system: str = Field(default="", description="System instructions")
    prompt: str = Field(description="User prompt (expressions allowed)")
    response_format: Literal["text", "json"] = "text"
    output_schema: dict[str, Any] | None = Field(default=None, description="JSON schema (json format)")
    model: ModelSettings = Field(default_factory=ModelSettings)


class LLMPromptNode(_AINode):
    type = "ai.prompt"
    label = "LLM prompt"
    description = "Send a prompt to any configured model; optionally enforce a JSON schema."
    config_model = PromptConfig
    output_schema = {"type": "object", "properties": {"text": {"type": "string"}, "data": {}, "usage": USAGE_SCHEMA}}

    async def execute(self, ctx: NodeContext, config: PromptConfig) -> NodeResult:
        schema = config.output_schema if config.response_format == "json" else None
        if config.response_format == "json" and schema is None:
            schema = {"type": "object"}
        result = await _custom_prompt(ctx, config.system, config.prompt, schema, config.model)
        return Completed(output={"text": result.text, "data": result.data, "usage": result.usage()})


async def _custom_prompt(
    ctx: NodeContext, system: str, prompt: str, schema: dict[str, Any] | None, settings: ModelSettings
) -> AIResult:
    request = AIRequest(
        prompt=prompt,
        system=system or None,
        output_schema=schema,
        models=settings.refs(),
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        effort=settings.effort,
        max_repair_attempts=settings.max_repair_attempts,
        timeout=min(ctx.timeout, 300),
        prompt_key=f"inline:{ctx.node.id}",
    )
    try:
        result = await ctx.runtime.ai().complete(request)
    except AIError as exc:
        raise NodeError(
            f"AI call failed: {exc.message}", retryable=exc.retryable, details={"attempts": exc.attempts}
        ) from exc
    ctx.log(
        "info",
        f"AI {result.provider}/{result.model}: {result.input_tokens}+{result.output_tokens} tokens",
        {"cost_usd": result.cost_usd},
    )
    return result


class ExtractField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    type: Literal["string", "number", "integer", "boolean", "date", "array", "object"] = "string"
    description: str = ""
    required: bool = True


def fields_to_schema(fields: list[ExtractField]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for f in fields:
        if f.type == "date":
            spec: dict[str, Any] = {"type": ["string", "null"], "description": (f.description + " (ISO 8601)").strip()}
        elif f.type == "array":
            spec = {"type": ["array", "null"], "items": {"type": "string"}}
        elif f.type == "object":
            spec = {"type": ["object", "null"]}
        else:
            spec = {"type": [f.type, "null"]}
        if f.description and "description" not in spec:
            spec["description"] = f.description
        props[f.name] = spec
    return {"type": "object", "properties": props, "required": [f.name for f in fields], "additionalProperties": False}


class ExtractConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Any = Field(description="Source text")
    fields: list[ExtractField] = Field(default_factory=list, description="Fields to extract")
    output_schema: dict[str, Any] | None = Field(default=None, description="Full JSON schema (overrides fields)")
    instructions: str = "Extract the requested fields from the text."
    model: ModelSettings = Field(default_factory=ModelSettings)


class StructuredExtractionNode(_AINode):
    type = "ai.extract"
    label = "Structured extraction"
    description = "Extract fields into a validated JSON object."
    config_model = ExtractConfig
    output_schema = {"type": "object", "properties": {"data": {"type": "object"}, "usage": USAGE_SCHEMA}}

    async def execute(self, ctx: NodeContext, config: ExtractConfig) -> NodeResult:
        schema = config.output_schema or (fields_to_schema(config.fields) if config.fields else None)
        if schema is None:
            raise NodeError("Define fields or an output_schema")
        result = await run_prompt(
            ctx,
            "extract",
            config.model,
            {"text": stringify(config.text), "instructions": config.instructions},
            output_schema=schema,
        )
        return Completed(output={"data": result.data, "usage": result.usage()})


class Category(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,50}$")
    description: str = ""


class ClassifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Any
    categories: list[Category] = Field(min_length=2, max_length=50)
    multi_label: bool = False
    route_by_category: bool = Field(default=False, description="Expose one output handle per category")
    confidence_threshold: float | None = Field(default=None, ge=0, le=1, description="Below this, route to 'uncertain'")
    model: ModelSettings = Field(default_factory=ModelSettings)


class ClassificationNode(_AINode):
    type = "ai.classify"
    label = "Classification"
    description = "Classify text into your categories with confidence; optionally route by category."
    config_model = ClassifyConfig
    output_schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "categories": {"type": "array"},
            "confidence": {"type": "number"},
            "usage": USAGE_SCHEMA,
        },
    }

    def handles(self, config: dict[str, Any]) -> list[str]:
        if not config.get("route_by_category"):
            return ["out"]
        names = [c.get("name") for c in config.get("categories", []) if isinstance(c, dict)]
        return names + (["uncertain"] if config.get("confidence_threshold") is not None else [])

    async def execute(self, ctx: NodeContext, config: ClassifyConfig) -> NodeResult:
        names = [c.name for c in config.categories]
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                ("categories" if config.multi_label else "category"): (
                    {"type": "array", "items": {"type": "string", "enum": names}, "minItems": 1}
                    if config.multi_label
                    else {"type": "string", "enum": names}
                ),
                "confidence": CONFIDENCE,
                "reasoning_summary": REASONING,
            },
            "required": [("categories" if config.multi_label else "category"), "confidence", "reasoning_summary"],
            "additionalProperties": False,
        }
        listing = "\n".join(
            f"- {c.name}: {c.description}" if c.description else f"- {c.name}" for c in config.categories
        )
        result = await run_prompt(
            ctx,
            "classify",
            config.model,
            {"text": stringify(config.text), "category_list": listing, "multi_label": config.multi_label},
            output_schema=schema,
        )
        data = dict(result.data)
        chosen = data.get("categories") or [data.get("category")]
        output = {**data, "category": chosen[0], "categories": chosen, "usage": result.usage()}
        branches = None
        if config.route_by_category:
            low = config.confidence_threshold is not None and data["confidence"] < config.confidence_threshold
            branches = ["uncertain"] if low else chosen
        return Completed(output=output, branches=branches)


class SummarizeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Any
    style: Literal["paragraph", "bullets", "executive", "ticket_resolution"] = "paragraph"
    max_words: int = Field(default=150, ge=10, le=2000)
    focus: str | None = None
    model: ModelSettings = Field(default_factory=ModelSettings)


class SummarizationNode(_AINode):
    type = "ai.summarize"
    label = "Summarization"
    description = "Summarise text with key points."
    config_model = SummarizeConfig
    output_schema = SUMMARY_SCHEMA

    async def execute(self, ctx: NodeContext, config: SummarizeConfig) -> NodeResult:
        result = await run_prompt(
            ctx,
            "summarize",
            config.model,
            {
                "text": stringify(config.text),
                "style": config.style,
                "max_words": config.max_words,
                "focus": config.focus,
            },
        )
        return Completed(output={**result.data, "usage": result.usage()})


class SentimentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Any
    model: ModelSettings = Field(default_factory=ModelSettings)


class SentimentNode(_AINode):
    type = "ai.sentiment"
    label = "Sentiment analysis"
    description = "Five-level sentiment with score, confidence and emotions."
    config_model = SentimentConfig
    output_schema = SENTIMENT_SCHEMA

    async def execute(self, ctx: NodeContext, config: SentimentConfig) -> NodeResult:
        result = await run_prompt(ctx, "sentiment", config.model, {"text": stringify(config.text)})
        return Completed(output={**result.data, "usage": result.usage()})


class DocumentAnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str | None = Field(default=None, description="Stored file (PDF, text, xlsx)")
    text: str | None = Field(default=None, description="Document text (alternative to file_id)")
    instructions: str = "Summarise the document and list obligations, deadlines, amounts and risks."
    output_schema: dict[str, Any] | None = None
    max_chars: int = Field(default=150_000, ge=1000, le=900_000)
    model: ModelSettings = Field(default_factory=ModelSettings)


async def _document_text(
    ctx: NodeContext, file_id: str | None, text: str | None, max_chars: int
) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {}
    if file_id:
        meta, content = await ctx.runtime.files.load(file_id)
        text = extract_text(content, meta.get("content_type", ""), meta.get("filename", ""))
    if not text:
        raise NodeError("Provide file_id or text")
    if len(text) > max_chars:
        raise NodeError(
            f"Document has {len(text)} characters, above max_chars={max_chars}; raise the limit or "
            "split the document upstream",
            code="document_too_large",
        )
    return text, meta


class DocumentAnalysisNode(_AINode):
    type = "ai.document_analysis"
    label = "Document analysis"
    description = "Analyse a document (PDF/text) with your instructions; optional structured output."
    config_model = DocumentAnalysisConfig
    output_schema = {"type": "object", "properties": {"analysis": {"type": "string"}, "data": {}, "file": {}}}

    async def execute(self, ctx: NodeContext, config: DocumentAnalysisConfig) -> NodeResult:
        text, meta = await _document_text(ctx, config.file_id, config.text, config.max_chars)
        result = await run_prompt(
            ctx,
            "document_analysis",
            config.model,
            {"text": text, "instructions": config.instructions},
            output_schema=config.output_schema,
            use_library_schema=False,
        )
        return Completed(
            output={
                "analysis": result.text if result.data is None else None,
                "data": result.data,
                "file": meta or None,
                "characters": len(text),
                "usage": result.usage(),
            }
        )


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    texts: Any = Field(description="String or list of strings")
    connection_id: str | None = Field(default=None, json_schema_extra={"x-connection": "category:ai"})
    model: str | None = None


class EmbeddingNode(_AINode):
    type = "ai.embedding"
    label = "Embedding generation"
    description = "Create vector embeddings (OpenAI, Gemini or local models)."
    config_model = EmbeddingConfig
    output_schema = {"type": "object", "properties": {"vectors": {"type": "array"}, "dimensions": {"type": "integer"}}}

    async def execute(self, ctx: NodeContext, config: EmbeddingConfig) -> NodeResult:
        texts = config.texts if isinstance(config.texts, list) else [config.texts]
        texts = [stringify(t) for t in texts]
        if not texts or len(texts) > 2048:
            raise NodeError("Provide between 1 and 2048 texts")
        try:
            resp, provider, cost = await ctx.runtime.ai().embed(ModelRef(config.connection_id, config.model), texts)
        except AIError as exc:
            raise NodeError(f"Embedding failed: {exc.message}", retryable=exc.retryable) from exc
        return Completed(
            output={
                "vectors": resp.vectors,
                "count": len(resp.vectors),
                "dimensions": len(resp.vectors[0]) if resp.vectors else 0,
                "usage": {
                    "provider": provider,
                    "model": resp.model,
                    "input_tokens": resp.input_tokens,
                    "cost_usd": cost,
                },
            }
        )


class DecisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(description="What the AI must decide, e.g. 'Recommend refund amount and whether to approve'")
    policy: str | None = Field(default=None, description="Business rules the decision must respect")
    context: Any = Field(default=None, description="Data for the decision")
    options: list[str] = Field(default_factory=list, description="Allowed decisions (enum); empty = free text")
    output_schema: dict[str, Any] | None = Field(
        default=None, description="Extra fields to return (JSON schema properties object)"
    )
    route_by_decision: bool = False
    min_confidence: float | None = Field(default=None, ge=0, le=1, description="Below this, route to 'needs_review'")
    model: ModelSettings = Field(default_factory=ModelSettings)


class AIDecisionNode(_AINode):
    type = "ai.decision"
    label = "AI decision"
    description = "Goal-driven decision with a validated output schema, confidence and optional routing."
    config_model = DecisionConfig
    output_schema = {
        "type": "object",
        "properties": {
            "decision": {"type": "string"},
            "confidence": {"type": "number"},
            "reasoning_summary": {"type": "string"},
        },
    }

    def handles(self, config: dict[str, Any]) -> list[str]:
        if not config.get("route_by_decision") or not config.get("options"):
            return ["out"]
        return list(config["options"]) + (["needs_review"] if config.get("min_confidence") is not None else [])

    async def execute(self, ctx: NodeContext, config: DecisionConfig) -> NodeResult:
        extra = (config.output_schema or {}).get("properties", config.output_schema or {})
        if not isinstance(extra, dict):
            raise NodeError("output_schema must be a JSON schema object or a properties map")
        props: dict[str, Any] = {
            "decision": {"type": "string", "enum": config.options} if config.options else {"type": "string"},
            "confidence": CONFIDENCE,
            "reasoning_summary": REASONING,
            **extra,
        }
        schema = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
        result = await run_prompt(
            ctx,
            "decision",
            config.model,
            {"goal": config.goal, "policy": config.policy, "options": config.options, "context": config.context},
            output_schema=schema,
        )
        data = dict(result.data)
        branches = None
        if config.route_by_decision and config.options:
            low = config.min_confidence is not None and data["confidence"] < config.min_confidence
            branches = ["needs_review"] if low else [data["decision"]]
        return Completed(output={**data, "usage": result.usage()}, branches=branches)


# ----------------------------------------------------------------------------- reusable components


class LeadClassifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead: Any = Field(description="Lead record (name, email, company, title, employees, message, ...)")
    model: ModelSettings = Field(default_factory=ModelSettings)


class LeadClassifierNode(_AINode):
    type = "ai.lead_classifier"
    label = "AI Lead Classifier"
    description = "Segment and prioritise a lead: classification, priority, confidence, reasoning summary."
    config_model = LeadClassifierConfig
    output_schema = LEAD_SCHEMA

    async def execute(self, ctx: NodeContext, config: LeadClassifierConfig) -> NodeResult:
        result = await run_prompt(ctx, "lead_classifier", config.model, {"lead": config.lead})
        return Completed(output={**result.data, "usage": result.usage()})


class EmailRouterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: str = Field(default="", description="From address")
    subject: str = ""
    body: str
    route: bool = Field(default=True, description="One output handle per category")
    model: ModelSettings = Field(default_factory=ModelSettings)


ROUTER_CATEGORIES = ["sales", "support", "billing", "complaint", "partnership", "spam", "other"]


class EmailRouterNode(_AINode):
    type = "ai.email_router"
    label = "AI Email Router"
    description = "Classify inbound email into sales/support/billing/complaint/partnership/spam/other and route it."
    config_model = EmailRouterConfig
    output_schema = EMAIL_ROUTER_SCHEMA

    def handles(self, config: dict[str, Any]) -> list[str]:
        return list(ROUTER_CATEGORIES) if config.get("route", True) else ["out"]

    async def execute(self, ctx: NodeContext, config: EmailRouterConfig) -> NodeResult:
        result = await run_prompt(
            ctx,
            "email_router",
            config.model,
            {"sender": config.sender, "subject": config.subject, "body": config.body[:50_000]},
        )
        data = result.data
        return Completed(
            output={**data, "usage": result.usage()}, branches=[data["category"]] if config.route else None
        )


class DocumentExtractorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str | None = None
    text: str | None = None
    max_chars: int = Field(default=150_000, ge=1000, le=900_000)
    model: ModelSettings = Field(default_factory=ModelSettings)


class DocumentExtractorNode(_AINode):
    type = "ai.document_extractor"
    label = "AI Document Extractor"
    description = "Extract structured fields from invoices, contracts and purchase orders."
    config_model = DocumentExtractorConfig
    output_schema = DOCUMENT_SCHEMA

    async def execute(self, ctx: NodeContext, config: DocumentExtractorConfig) -> NodeResult:
        text, meta = await _document_text(ctx, config.file_id, config.text, config.max_chars)
        result = await run_prompt(ctx, "document_extractor", config.model, {"text": text})
        return Completed(output={**result.data, "file": meta or None, "usage": result.usage()})


AI_NODES: list[type[NodeType]] = [
    LLMPromptNode,
    StructuredExtractionNode,
    ClassificationNode,
    SummarizationNode,
    SentimentNode,
    DocumentAnalysisNode,
    EmbeddingNode,
    AIDecisionNode,
    LeadClassifierNode,
    EmailRouterNode,
    DocumentExtractorNode,
]
