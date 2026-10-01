"""AI administration: versioned prompt library and usage/cost reporting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import jsonschema
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.exc import IntegrityError

from app.ai.pricing import DEFAULT_PRICING
from app.ai.prompts import BUILTIN_PROMPTS, publish_prompt_version
from app.api.deps import PrincipalDep, SessionDep, require_anywhere
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.principal import Principal
from app.core.rbac import Permission
from app.db.models import AICall, Execution, PromptTemplate, Workflow
from app.services import audit

router = APIRouter(prefix="/ai", tags=["ai"])


class PromptVersionIn(BaseModel):
    description: str = Field(default="", max_length=2000)
    system_prompt: str = Field(default="", max_length=50_000)
    user_prompt: str = Field(min_length=1, max_length=100_000)
    output_schema: dict[str, Any] | None = None
    model_settings: dict[str, Any] = Field(default_factory=dict)


def _prompt_out(p: PromptTemplate) -> dict[str, Any]:
    return {
        "key": p.key,
        "version": p.version,
        "description": p.description,
        "system_prompt": p.system_prompt,
        "user_prompt": p.user_prompt,
        "output_schema": p.output_schema,
        "model_settings": p.model_settings,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat(),
        "builtin": False,
    }


@router.get("/prompts")
async def list_prompts(principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(PromptTemplate.key, func.max(PromptTemplate.version), func.count())
            .where(PromptTemplate.org_id == principal.org_id)
            .group_by(PromptTemplate.key)
        )
    ).all()
    custom = {k: (v, c) for k, v, c in rows}
    keys = sorted(set(BUILTIN_PROMPTS) | set(custom))
    return [
        {
            "key": k,
            "description": BUILTIN_PROMPTS[k].description if k in BUILTIN_PROMPTS else "",
            "builtin": k in BUILTIN_PROMPTS,
            "latest_version": custom.get(k, (0, 0))[0],
            "custom_versions": custom.get(k, (0, 0))[1],
        }
        for k in keys
    ]


@router.get("/prompts/{key}/versions")
async def prompt_versions(key: str, principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(PromptTemplate)
                .where(PromptTemplate.org_id == principal.org_id, PromptTemplate.key == key)
                .order_by(PromptTemplate.version.desc())
            )
        )
        .scalars()
        .all()
    )
    out = [_prompt_out(r) for r in rows]
    if key in BUILTIN_PROMPTS:
        b = BUILTIN_PROMPTS[key]
        out.append(
            {
                "key": key,
                "version": 0,
                "description": b.description,
                "system_prompt": b.system,
                "user_prompt": b.user,
                "output_schema": b.output_schema,
                "model_settings": b.model_settings,
                "is_active": True,
                "created_at": None,
                "builtin": True,
            }
        )
    if not out:
        raise NotFoundError("Prompt not found")
    return out


@router.post("/prompts/{key}/versions", status_code=201)
async def create_prompt_version(
    key: Annotated[str, Path(pattern=r"^[a-z0-9_.-]{1,120}$")],
    body: PromptVersionIn,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_anywhere(Permission.AI_PROMPTS_MANAGE))],
) -> dict[str, Any]:
    if body.output_schema is not None:
        try:
            jsonschema.Draft202012Validator.check_schema(body.output_schema)
        except jsonschema.SchemaError as exc:
            raise ValidationFailedError(f"Invalid output schema: {exc.message}") from exc
    try:
        row = await publish_prompt_version(
            session,
            principal.org_id,
            key,
            system=body.system_prompt,
            user=body.user_prompt,
            output_schema=body.output_schema,
            model_settings=body.model_settings,
            description=body.description,
            created_by=principal.user_id,
        )
    except IntegrityError as exc:
        raise ConflictError("Concurrent prompt publish; retry") from exc
    await audit.record(
        session,
        principal,
        "ai.prompt_published",
        resource_type="prompt",
        resource_id=f"{key}@{row.version}",
        summary=f"Prompt '{key}' v{row.version} published",
    )
    return _prompt_out(row)


@router.post("/prompts/{key}/versions/{version}/{state}")
async def set_prompt_state(
    key: str,
    version: int,
    state: Literal["activate", "deactivate"],
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_anywhere(Permission.AI_PROMPTS_MANAGE))],
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(PromptTemplate).where(
                PromptTemplate.org_id == principal.org_id, PromptTemplate.key == key, PromptTemplate.version == version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Prompt version not found")
    row.is_active = state == "activate"
    await audit.record(
        session,
        principal,
        f"ai.prompt_{state}d",
        resource_type="prompt",
        resource_id=f"{key}@{version}",
        summary=f"Prompt '{key}' v{version} {state}d",
    )
    return _prompt_out(row)


@router.get("/pricing")
async def pricing(principal: PrincipalDep) -> dict[str, Any]:
    return {
        "currency": "USD",
        "unit": "per 1M tokens",
        "models": {m: {"input": i, "output": o} for m, (i, o) in DEFAULT_PRICING.items()},
    }


@router.get("/usage")
async def usage(
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_anywhere(Permission.DASHBOARD_READ))],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    group_by: Literal["model", "provider", "workflow", "day", "prompt"] = "model",
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    base = select(AICall).where(AICall.org_id == principal.org_id, AICall.created_at >= since)
    allowed = principal.workspaces_with(Permission.DASHBOARD_READ)
    if allowed is not None:
        base = base.join(Execution, Execution.id == AICall.execution_id).where(
            Execution.workspace_id.in_(allowed or [])
        )
    sub = base.subquery()
    if group_by == "workflow":
        key_col = Workflow.name
        stmt = (
            select(key_col.label("k"), *_aggs(sub))
            .select_from(sub)
            .join(Execution, Execution.id == sub.c.execution_id, isouter=True)
            .join(Workflow, Workflow.id == Execution.workflow_id, isouter=True)
            .group_by(key_col)
        )
    else:
        key_col = {
            "model": sub.c.model,
            "provider": sub.c.provider,
            "prompt": sub.c.prompt_key,
            "day": func.date_trunc("day", sub.c.created_at),
        }[group_by]
        stmt = select(key_col.label("k"), *_aggs(sub)).group_by(key_col)
    rows = (await session.execute(stmt.order_by(func.sum(sub.c.cost_usd).desc()))).all()
    groups = [
        {
            "key": (r.k.isoformat() if hasattr(r.k, "isoformat") else r.k) or "unknown",
            "calls": r.calls,
            "errors": r.errors or 0,
            "input_tokens": r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
            "cost_usd": float(r.cost or 0),
            "avg_latency_ms": round(float(r.latency or 0)),
            "fallbacks": r.fallbacks or 0,
        }
        for r in rows
    ]
    totals = {
        k: sum(g[k] for g in groups)
        for k in ("calls", "errors", "input_tokens", "output_tokens", "cost_usd", "fallbacks")
    }
    return {"since": since.isoformat(), "group_by": group_by, "totals": totals, "groups": groups}


def _aggs(sub: Any) -> list[Any]:
    return [
        func.count().label("calls"),
        func.sum(cast(sub.c.status != "success", Integer)).label("errors"),
        func.sum(sub.c.input_tokens).label("input_tokens"),
        func.sum(sub.c.output_tokens).label("output_tokens"),
        func.sum(sub.c.cost_usd).label("cost"),
        func.avg(sub.c.latency_ms).label("latency"),
        func.sum(cast(sub.c.is_fallback, Integer)).label("fallbacks"),
    ]
