"""Template marketplace: seeding built-in templates and installing them into workspaces."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.principal import Principal
from app.db.models import Connection, Workflow, WorkflowTemplate
from app.services import workflows as wf_service
from app.templates.builder import placeholders, substitute
from app.templates.library import all_templates


async def seed_templates(session: AsyncSession) -> int:
    """Upsert built-in templates (idempotent; safe on every startup)."""
    count = 0
    for spec in all_templates():
        row = (
            await session.execute(select(WorkflowTemplate).where(WorkflowTemplate.slug == spec.slug))
        ).scalar_one_or_none()
        definition = spec.definition()
        fields = {
            "name": spec.name,
            "category": spec.category,
            "summary": spec.summary,
            "description": spec.description,
            "tags": spec.tags,
            "required_connectors": sorted(f"{k}={v}" for k, v in spec.connection_roles.items()),
            "definition": definition,
            "is_featured": spec.featured,
        }
        if row is None:
            session.add(WorkflowTemplate(slug=spec.slug, **fields))
        else:
            for k, v in fields.items():
                setattr(row, k, v)
        count += 1
    await session.flush()
    return count


def connection_roles(template: WorkflowTemplate) -> dict[str, str]:
    return dict(item.split("=", 1) for item in template.required_connectors)


async def install(
    session: AsyncSession,
    principal: Principal,
    slug: str,
    *,
    workspace_id: uuid.UUID,
    name: str | None,
    connections: dict[str, str],
) -> Workflow:
    template = (
        await session.execute(select(WorkflowTemplate).where(WorkflowTemplate.slug == slug))
    ).scalar_one_or_none()
    if template is None:
        raise NotFoundError("Template not found")
    roles = connection_roles(template)
    unknown = set(connections) - set(roles)
    if unknown:
        raise ValidationFailedError(f"Unknown connection roles: {', '.join(sorted(unknown))}")
    mapping: dict[str, str] = {}
    for role, cid in connections.items():
        if not cid:
            continue
        try:
            conn_uuid = uuid.UUID(cid)
        except ValueError as exc:
            raise ValidationFailedError(f"Invalid connection id for role '{role}'") from exc
        conn = (
            await session.execute(
                select(Connection).where(
                    Connection.id == conn_uuid,
                    Connection.org_id == principal.org_id,
                    Connection.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()
        if conn is None:
            raise ValidationFailedError(f"Connection for role '{role}' not found in this workspace")
        mapping[role] = str(conn.id)
    definition = substitute(template.definition, mapping)
    wf, _ = await wf_service.create_workflow(
        session,
        principal,
        workspace_id=workspace_id,
        name=name or template.name,
        description=template.summary,
        tags=list(template.tags),
        definition=definition,
        template_slug=template.slug,
    )
    template.install_count += 1
    return wf


def template_out(t: WorkflowTemplate, *, include_definition: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "slug": t.slug,
        "name": t.name,
        "category": t.category,
        "summary": t.summary,
        "description": t.description,
        "tags": t.tags,
        "connection_roles": connection_roles(t),
        "featured": t.is_featured,
        "install_count": t.install_count,
        "node_count": len(t.definition.get("nodes", [])),
        "node_types": sorted({n["type"] for n in t.definition.get("nodes", [])}),
        "unmapped_roles": sorted(placeholders(t.definition)),
    }
    if include_definition:
        out["definition"] = t.definition
    return out
