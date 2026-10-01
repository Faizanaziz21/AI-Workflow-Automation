"""Workflow lifecycle and versioning.

Version model
* Each workflow has at most one **draft** version (mutable, optimistic-concurrency ``revision``).
* **Publishing** freezes the draft into an immutable **published** version (definition hash stored); the
  previously published version becomes **archived**. Executions reference the exact version they started on.
* Editing after publish lazily creates a new draft (``latest_version + 1``) copied from the published one.
* **Rollback** republishes an older definition as a *new* version, preserving history immutability.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailedError
from app.core.principal import Principal
from app.db.enums import VersionStatus, WorkflowStatus
from app.db.models import Connection, Workflow, WorkflowTrigger, WorkflowVersion
from app.engine.definition import WorkflowDefinition, definition_hash
from app.engine.validation import connection_ids_in, validate_definition
from app.services import audit
from app.services.connections import can_use_connection, write_secret

EMPTY_DEFINITION: dict[str, Any] = {
    "schema_version": 1,
    "nodes": [
        {
            "id": "trigger",
            "type": "trigger.manual",
            "name": "Manual trigger",
            "config": {},
            "position": {"x": 80, "y": 200},
        }
    ],
    "edges": [],
    "settings": {},
    "variables": {},
}


def normalize(definition: dict[str, Any]) -> dict[str, Any]:
    try:
        return WorkflowDefinition.model_validate(definition).model_dump(mode="json")
    except ValidationError as exc:
        details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        raise ValidationFailedError("Invalid workflow definition", details=details) from exc


async def get_workflow(
    session: AsyncSession,
    principal: Principal,
    workflow_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
    *,
    lock: bool = False,
) -> Workflow:
    stmt = select(Workflow).where(Workflow.id == workflow_id, Workflow.org_id == principal.org_id)
    if workspace_id:
        stmt = stmt.where(Workflow.workspace_id == workspace_id)
    if lock:
        stmt = stmt.with_for_update()
    wf = (await session.execute(stmt)).scalar_one_or_none()
    if wf is None or not principal.can_access_workspace(wf.workspace_id):
        raise NotFoundError("Workflow not found")
    return wf


async def get_version(session: AsyncSession, wf: Workflow, version: int) -> WorkflowVersion:
    row = (
        await session.execute(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == wf.id, WorkflowVersion.version == version)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Version {version} not found")
    return row


async def get_draft(session: AsyncSession, wf: Workflow) -> WorkflowVersion | None:
    return (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == wf.id, WorkflowVersion.status == VersionStatus.DRAFT.value
            )
        )
    ).scalar_one_or_none()


async def get_published(session: AsyncSession, wf: Workflow) -> WorkflowVersion | None:
    if not wf.published_version_id:
        return None
    return await session.get(WorkflowVersion, wf.published_version_id)


async def create_workflow(
    session: AsyncSession,
    principal: Principal,
    *,
    workspace_id: uuid.UUID,
    name: str,
    description: str = "",
    tags: list[str] | None = None,
    definition: dict[str, Any] | None = None,
    template_slug: str | None = None,
) -> tuple[Workflow, WorkflowVersion]:
    normalized = normalize(definition or EMPTY_DEFINITION)
    wf = Workflow(
        id=uuid.uuid4(),
        org_id=principal.org_id,
        workspace_id=workspace_id,
        name=name,
        description=description,
        tags=tags or [],
        latest_version=1,
        template_slug=template_slug,
        created_by=principal.user_id,
        updated_by=principal.user_id,
    )
    session.add(wf)
    await session.flush()
    draft = WorkflowVersion(
        org_id=principal.org_id,
        workflow_id=wf.id,
        version=1,
        status=VersionStatus.DRAFT.value,
        definition=normalized,
        definition_hash=definition_hash(normalized),
        created_by=principal.user_id,
    )
    session.add(draft)
    await session.flush()
    await audit.record(
        session,
        principal,
        "workflow.created",
        resource_type="workflow",
        resource_id=wf.id,
        workspace_id=workspace_id,
        summary=f"Workflow '{name}' created",
        details={"template": template_slug} if template_slug else {},
    )
    return wf, draft


async def ensure_draft(session: AsyncSession, principal: Principal, wf: Workflow) -> WorkflowVersion:
    draft = await get_draft(session, wf)
    if draft is not None:
        return draft
    base = await get_published(session, wf)
    definition = base.definition if base else normalize(EMPTY_DEFINITION)
    wf.latest_version += 1
    draft = WorkflowVersion(
        org_id=wf.org_id,
        workflow_id=wf.id,
        version=wf.latest_version,
        status=VersionStatus.DRAFT.value,
        definition=definition,
        definition_hash=definition_hash(definition),
        created_by=principal.user_id,
    )
    session.add(draft)
    await session.flush()
    return draft


async def save_draft(
    session: AsyncSession,
    principal: Principal,
    wf: Workflow,
    definition: dict[str, Any],
    expected_revision: int | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> WorkflowVersion:
    if wf.status == WorkflowStatus.ARCHIVED.value:
        raise ConflictError("Workflow is archived; restore it before editing")
    normalized = normalize(definition)
    existing = await get_draft(session, wf)
    if existing is not None and expected_revision is not None and existing.revision != expected_revision:
        raise ConflictError(
            f"Draft was modified by someone else (revision {existing.revision}); reload and retry",
            code="revision_conflict",
            details={"current_revision": existing.revision},
        )
    draft = existing or await ensure_draft(session, principal, wf)
    new_hash = definition_hash(normalized)
    if new_hash != draft.definition_hash:
        draft.definition = normalized
        draft.definition_hash = new_hash
        draft.revision += 1
        draft.updated_at = datetime.now(UTC)
    if name:
        wf.name = name
    if description is not None:
        wf.description = description
    wf.updated_by = principal.user_id
    wf.updated_at = datetime.now(UTC)
    await audit.record(
        session,
        principal,
        "workflow.modified",
        resource_type="workflow",
        resource_id=wf.id,
        workspace_id=wf.workspace_id,
        summary=f"Draft v{draft.version} of '{wf.name}' saved",
        details={
            "version": draft.version,
            "revision": draft.revision,
            "nodes": len(normalized["nodes"]),
            "edges": len(normalized["edges"]),
        },
    )
    return draft


async def _check_connections(
    session: AsyncSession, principal: Principal, wf: Workflow, definition: WorkflowDefinition
) -> None:
    ids = connection_ids_in(definition)
    if not ids:
        return
    try:
        uuids = [uuid.UUID(i) for i in ids]
    except ValueError as exc:
        raise ValidationFailedError("Invalid connection id in definition") from exc
    rows = (
        (
            await session.execute(
                select(Connection).where(Connection.id.in_(uuids), Connection.org_id == principal.org_id)
            )
        )
        .scalars()
        .all()
    )
    found = {str(r.id): r for r in rows}
    for cid in ids:
        conn = found.get(cid)
        if conn is None or conn.workspace_id != wf.workspace_id:
            raise ValidationFailedError(f"Connection {cid} does not exist in this workspace")
        if not can_use_connection(principal, conn):
            raise PermissionDeniedError(f"You are not allowed to use connection '{conn.name}'")


async def publish(session: AsyncSession, principal: Principal, wf: Workflow, change_note: str = "") -> WorkflowVersion:
    if wf.status == WorkflowStatus.ARCHIVED.value:
        raise ConflictError("Workflow is archived")
    draft = await get_draft(session, wf)
    if draft is None:
        raise ConflictError("Nothing to publish: no draft changes since the last publish")
    report = validate_definition(draft.definition)
    if not report.valid:
        raise ValidationFailedError("Workflow has validation errors", details=report.as_dict())
    definition = WorkflowDefinition.model_validate(draft.definition)
    await _check_connections(session, principal, wf, definition)
    now = datetime.now(UTC)
    await session.execute(
        update(WorkflowVersion)
        .where(
            WorkflowVersion.workflow_id == wf.id,
            WorkflowVersion.status == VersionStatus.PUBLISHED.value,
        )
        .values(status=VersionStatus.ARCHIVED.value)
    )
    draft.status = VersionStatus.PUBLISHED.value
    draft.published_at = now
    draft.published_by = principal.user_id
    draft.change_note = change_note
    wf.published_version_id = draft.id
    wf.updated_at = now
    await session.flush()
    await sync_trigger(session, wf, draft, definition)
    await audit.record(
        session,
        principal,
        "workflow.published",
        resource_type="workflow",
        resource_id=wf.id,
        workspace_id=wf.workspace_id,
        summary=f"'{wf.name}' v{draft.version} published",
        details={
            "version": draft.version,
            "hash": draft.definition_hash,
            "note": change_note,
            "warnings": len(report.issues),
        },
    )
    return draft


async def rollback(
    session: AsyncSession, principal: Principal, wf: Workflow, to_version: int, change_note: str = ""
) -> WorkflowVersion:
    target = await get_version(session, wf, to_version)
    if target.status == VersionStatus.DRAFT.value:
        raise ValidationFailedError("Cannot roll back to a draft")
    draft = await ensure_draft(session, principal, wf)
    draft.definition = target.definition
    draft.definition_hash = target.definition_hash
    draft.revision += 1
    version = await publish(session, principal, wf, change_note or f"Rollback to v{to_version}")
    await audit.record(
        session,
        principal,
        "workflow.rolled_back",
        resource_type="workflow",
        resource_id=wf.id,
        workspace_id=wf.workspace_id,
        summary=f"'{wf.name}' rolled back to v{to_version} as v{version.version}",
        details={"from_version": to_version, "new_version": version.version},
    )
    return version


async def clone(
    session: AsyncSession,
    principal: Principal,
    wf: Workflow,
    name: str | None = None,
    workspace_id: uuid.UUID | None = None,
) -> Workflow:
    source = await get_draft(session, wf) or await get_published(session, wf)
    definition = source.definition if source else EMPTY_DEFINITION
    new_wf, _ = await create_workflow(
        session,
        principal,
        workspace_id=workspace_id or wf.workspace_id,
        name=name or f"{wf.name} (copy)",
        description=wf.description,
        tags=list(wf.tags),
        definition=definition,
    )
    await audit.record(
        session,
        principal,
        "workflow.cloned",
        resource_type="workflow",
        resource_id=new_wf.id,
        workspace_id=new_wf.workspace_id,
        summary=f"Cloned from '{wf.name}'",
        details={"source_workflow_id": str(wf.id)},
    )
    return new_wf


async def set_archived(session: AsyncSession, principal: Principal, wf: Workflow, archived: bool) -> Workflow:
    wf.status = WorkflowStatus.ARCHIVED.value if archived else WorkflowStatus.ACTIVE.value
    await session.execute(
        update(WorkflowTrigger).where(WorkflowTrigger.workflow_id == wf.id).values(enabled=not archived)
    )
    await audit.record(
        session,
        principal,
        "workflow.archived" if archived else "workflow.restored",
        resource_type="workflow",
        resource_id=wf.id,
        workspace_id=wf.workspace_id,
        summary=f"'{wf.name}' {'archived' if archived else 'restored'}",
    )
    return wf


# ----------------------------------------------------------------------------- triggers


def next_cron_run(cron: str, tz: str, after: datetime | None = None) -> datetime:
    zone = ZoneInfo(tz or "UTC")
    base = (after or datetime.now(UTC)).astimezone(zone)
    nxt: datetime = croniter(cron, base).get_next(datetime)
    return nxt.astimezone(UTC)


async def sync_trigger(
    session: AsyncSession, wf: Workflow, version: WorkflowVersion, definition: WorkflowDefinition
) -> WorkflowTrigger:
    node = next(n for n in definition.nodes if n.type.startswith("trigger.") and not n.disabled)
    trig = (
        await session.execute(select(WorkflowTrigger).where(WorkflowTrigger.workflow_id == wf.id))
    ).scalar_one_or_none()
    trigger_type = node.type.removeprefix("trigger.")
    if trig is None:
        trig = WorkflowTrigger(
            org_id=wf.org_id,
            workspace_id=wf.workspace_id,
            workflow_id=wf.id,
            workflow_version_id=version.id,
            trigger_type=trigger_type,
            node_id=node.id,
            state={},
        )
        session.add(trig)
    previous_config = dict(trig.config or {})
    type_changed = trig.trigger_type != trigger_type
    trig.workflow_version_id = version.id
    trig.trigger_type = trigger_type
    trig.node_id = node.id
    trig.config = node.config
    trig.enabled = wf.status == WorkflowStatus.ACTIVE.value
    trig.event_name = None
    trig.next_run_at = None
    trig.last_error = None
    cfg = node.config
    if trigger_type == "webhook":
        if not trig.webhook_token:
            trig.webhook_token = secrets.token_urlsafe(24)
        if cfg.get("authentication", "signature") == "signature" and not trig.signing_secret_id:
            secret = await write_secret(session, wf.org_id, {"signing_secret": "whsec_" + secrets.token_urlsafe(32)})
            trig.signing_secret_id = secret.id
    elif trigger_type == "schedule":
        trig.next_run_at = next_cron_run(cfg["cron"], cfg.get("timezone", "UTC"))
    elif trigger_type == "api_event":
        trig.event_name = cfg["event_name"]
    elif trigger_type == "file_uploaded" and cfg.get("source", "platform") == "platform":
        trig.event_name = "file.uploaded"
    if trigger_type in ("email", "db_change") or (
        trigger_type == "file_uploaded" and cfg.get("source") == "google_drive"
    ):
        trig.next_run_at = datetime.now(UTC)
        source_keys = ("connection_id", "table", "cursor_column", "folder", "folder_id", "source")
        if type_changed or any(previous_config.get(k) != cfg.get(k) for k in source_keys):
            trig.state = {}  # polling source changed: restart cursor
    await session.flush()
    return trig


# ----------------------------------------------------------------------------- diff


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict) and value:
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = value
    return out


def diff_definitions(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Structural diff from ``a`` (old) to ``b`` (new)."""
    na = {n["id"]: n for n in a.get("nodes", [])}
    nb = {n["id"]: n for n in b.get("nodes", [])}
    changed = []
    for nid in sorted(set(na) & set(nb)):
        fa = _flatten({k: v for k, v in na[nid].items() if k != "position"})
        fb = _flatten({k: v for k, v in nb[nid].items() if k != "position"})
        changes = [
            {"path": p, "before": fa.get(p), "after": fb.get(p)}
            for p in sorted(set(fa) | set(fb))
            if fa.get(p) != fb.get(p)
        ]
        moved = na[nid].get("position") != nb[nid].get("position")
        if changes or moved:
            changed.append({"id": nid, "type": nb[nid]["type"], "changes": changes, "moved": moved})

    def ekey(e: dict[str, Any]) -> tuple[str, str, str]:
        return (e["source"], e["target"], e.get("source_handle", "out"))

    ea = {ekey(e) for e in a.get("edges", [])}
    eb = {ekey(e) for e in b.get("edges", [])}
    sa, sb = _flatten(a.get("settings", {})), _flatten(b.get("settings", {}))
    return {
        "nodes_added": [{"id": i, "type": nb[i]["type"], "name": nb[i].get("name")} for i in sorted(set(nb) - set(na))],
        "nodes_removed": [
            {"id": i, "type": na[i]["type"], "name": na[i].get("name")} for i in sorted(set(na) - set(nb))
        ],
        "nodes_changed": changed,
        "edges_added": [{"source": s, "target": t, "source_handle": h} for s, t, h in sorted(eb - ea)],
        "edges_removed": [{"source": s, "target": t, "source_handle": h} for s, t, h in sorted(ea - eb)],
        "settings_changed": [
            {"path": p, "before": sa.get(p), "after": sb.get(p)}
            for p in sorted(set(sa) | set(sb))
            if sa.get(p) != sb.get(p)
        ],
        "variables_changed": a.get("variables") != b.get("variables"),
    }
