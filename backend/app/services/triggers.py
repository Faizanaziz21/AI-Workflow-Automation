"""Trigger dispatch: webhooks, API events, file uploads, cron and connector polling."""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.sdk import ConnectorError
from app.core.errors import AuthenticationError, NotFoundError, ValidationFailedError
from app.core.metrics import WEBHOOKS_RECEIVED
from app.core.principal import Principal
from app.core.security import verify_signature
from app.db.enums import TriggerType
from app.db.models import ApiKey, Execution, StoredFile, Workflow, WorkflowTrigger
from app.db.session import get_sessionmaker
from app.engine import executor
from app.engine.expressions import ExpressionError, evaluate, render, truthy
from app.services import connections as connection_service
from app.services.storage import file_ref
from app.services.workflows import next_cron_run

logger = logging.getLogger(__name__)

_FORWARDED_HEADERS_DENY = {"authorization", "cookie", "x-api-key", "proxy-authorization", "x-flowforge-signature"}


async def _start(
    session: AsyncSession,
    trig: WorkflowTrigger,
    trigger_type: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None,
    correlation_key: str | None = None,
    created_by: uuid.UUID | None = None,
) -> tuple[Execution, bool]:
    return await executor.start_execution(
        session,
        org_id=trig.org_id,
        workspace_id=trig.workspace_id,
        workflow_id=trig.workflow_id,
        version_id=trig.workflow_version_id,
        trigger_type=trigger_type,
        payload=payload,
        idempotency_key=idempotency_key,
        correlation_key=correlation_key,
        created_by=created_by,
    )


def _correlation(cfg: dict[str, Any], payload: dict[str, Any]) -> str | None:
    expr = cfg.get("correlation_key")
    if not expr:
        return None
    try:
        value = render(expr, payload)
    except ExpressionError:
        return None
    return str(value)[:200] if value not in (None, "") else None


# ----------------------------------------------------------------------------- webhooks


async def ingest_webhook(
    session: AsyncSession,
    token: str,
    *,
    method: str,
    headers: dict[str, str],
    query: dict[str, Any],
    body_bytes: bytes,
) -> tuple[Execution, bool, WorkflowTrigger]:
    trig = (
        await session.execute(
            select(WorkflowTrigger).where(
                WorkflowTrigger.webhook_token == token, WorkflowTrigger.trigger_type == "webhook"
            )
        )
    ).scalar_one_or_none()
    if trig is None or not trig.enabled:
        WEBHOOKS_RECEIVED.labels("not_found").inc()
        raise NotFoundError("Unknown webhook")
    cfg = trig.config or {}
    if method not in cfg.get("methods", ["POST"]):
        WEBHOOKS_RECEIVED.labels("method_not_allowed").inc()
        raise ValidationFailedError(f"Method {method} not allowed for this webhook", code="method_not_allowed")
    auth = cfg.get("authentication", "signature")
    lower = {k.lower(): v for k, v in headers.items()}
    if auth == "signature":
        secret = (await connection_service.read_secret(session, trig.signing_secret_id, trig.org_id)).get(
            "signing_secret"
        )
        if not secret or not verify_signature(
            secret, lower.get("x-flowforge-signature"), lower.get("x-flowforge-timestamp"), body_bytes
        ):
            WEBHOOKS_RECEIVED.labels("bad_signature").inc()
            raise AuthenticationError("Invalid or missing webhook signature", code="invalid_signature")
    elif auth == "api_key":
        raw = lower.get("x-api-key", "")
        prefix = raw[4:].split("_", 1)[0] if raw.startswith("ffk_") else ""
        key = (
            await session.execute(select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.org_id == trig.org_id))
        ).scalar_one_or_none()
        if (
            key is None
            or key.revoked_at
            or not hmac.compare_digest(key.key_hash, hashlib.sha256(raw.encode()).hexdigest())
        ):
            WEBHOOKS_RECEIVED.labels("bad_api_key").inc()
            raise AuthenticationError("Invalid API key for webhook")
    content_type = lower.get("content-type", "")
    body: Any
    if not body_bytes:
        body = None
    elif "json" in content_type or body_bytes.lstrip()[:1] in (b"{", b"["):
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError as exc:
            WEBHOOKS_RECEIVED.labels("bad_json").inc()
            raise ValidationFailedError("Body is not valid JSON") from exc
    elif "x-www-form-urlencoded" in content_type:
        from urllib.parse import parse_qsl

        body = dict(parse_qsl(body_bytes.decode(errors="replace")))
    else:
        body = body_bytes.decode(errors="replace")[:1_000_000]
    payload = {
        "body": body,
        "query": query,
        "method": method,
        "received_at": datetime.now(UTC).isoformat(),
        "headers": {k: v for k, v in lower.items() if k not in _FORWARDED_HEADERS_DENY},
    }
    idem_header = (cfg.get("idempotency_header") or "Idempotency-Key").lower()
    idem = lower.get(idem_header)
    ex, created = await _start(
        session,
        trig,
        TriggerType.WEBHOOK.value,
        payload,
        idempotency_key=f"webhook:{idem[:150]}" if idem else None,
        correlation_key=_correlation(cfg, payload),
    )
    WEBHOOKS_RECEIVED.labels("accepted" if created else "duplicate").inc()
    return ex, created, trig


# ----------------------------------------------------------------------------- API events


async def publish_event(
    session: AsyncSession,
    principal: Principal,
    name: str,
    data: Any,
    event_id: str | None,
    workspace_id: uuid.UUID | None,
) -> list[dict[str, Any]]:
    event_id = event_id or uuid.uuid4().hex
    stmt = select(WorkflowTrigger).where(
        WorkflowTrigger.org_id == principal.org_id,
        WorkflowTrigger.enabled.is_(True),
        WorkflowTrigger.trigger_type == "api_event",
        WorkflowTrigger.event_name == name,
    )
    if workspace_id is not None:
        stmt = stmt.where(WorkflowTrigger.workspace_id == workspace_id)
    allowed = principal.workspace_filter()
    results = []
    payload = {"event": name, "data": data, "event_id": event_id, "published_at": datetime.now(UTC).isoformat()}
    for trig in (await session.execute(stmt)).scalars().all():
        if allowed is not None and trig.workspace_id not in allowed:
            continue
        flt = (trig.config or {}).get("filter")
        if flt:
            try:
                expr = flt.strip()
                expr = expr[2:-2] if expr.startswith("{{") and expr.endswith("}}") else expr
                if not truthy(evaluate(expr, {"event": payload, "data": data})):
                    results.append({"workflow_id": str(trig.workflow_id), "status": "filtered"})
                    continue
            except ExpressionError as exc:
                results.append({"workflow_id": str(trig.workflow_id), "status": "filter_error", "error": exc.message})
                continue
        ex, created = await _start(
            session,
            trig,
            TriggerType.API_EVENT.value,
            payload,
            idempotency_key=f"event:{event_id}",
            created_by=principal.user_id,
        )
        results.append(
            {
                "workflow_id": str(trig.workflow_id),
                "execution_id": str(ex.id),
                "status": "started" if created else "duplicate",
            }
        )
    return results


async def dispatch_file_uploaded(session: AsyncSession, f: StoredFile, created_by: uuid.UUID | None) -> list[str]:
    triggers = (
        (
            await session.execute(
                select(WorkflowTrigger).where(
                    WorkflowTrigger.org_id == f.org_id,
                    WorkflowTrigger.workspace_id == f.workspace_id,
                    WorkflowTrigger.enabled.is_(True),
                    WorkflowTrigger.event_name == "file.uploaded",
                )
            )
        )
        .scalars()
        .all()
    )
    started = []
    for trig in triggers:
        pattern = (trig.config or {}).get("filename_pattern", "*") or "*"
        if not fnmatch.fnmatch(f.filename.lower(), pattern.lower()):
            continue
        ex, _ = await _start(
            session,
            trig,
            TriggerType.FILE_UPLOADED.value,
            {"file": file_ref(f), "source": "platform"},
            idempotency_key=f"file:{f.id}",
            created_by=created_by,
        )
        started.append(str(ex.id))
    return started


# ----------------------------------------------------------------------------- cron


async def fire_due_schedules(session: AsyncSession, limit: int = 100) -> int:
    now = datetime.now(UTC)
    due = (
        (
            await session.execute(
                select(WorkflowTrigger)
                .where(
                    WorkflowTrigger.enabled.is_(True),
                    WorkflowTrigger.trigger_type == "schedule",
                    WorkflowTrigger.next_run_at <= now,
                )
                .order_by(WorkflowTrigger.next_run_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for trig in due:
        cfg = trig.config or {}
        scheduled_for = trig.next_run_at or now
        try:
            await _start(
                session,
                trig,
                TriggerType.SCHEDULE.value,
                {"scheduled_for": scheduled_for.isoformat(), "payload": cfg.get("payload", {})},
                idempotency_key=f"schedule:{scheduled_for.isoformat()}",
            )
            trig.last_error = None
        except Exception as exc:
            logger.exception("schedule fire failed for trigger %s", trig.id)
            trig.last_error = str(exc)[:2000]
        trig.last_run_at = now
        # Missed ticks (downtime) collapse into one catch-up run; next run is the next future tick.
        trig.next_run_at = next_cron_run(cfg["cron"], cfg.get("timezone", "UTC"), after=max(now, scheduled_for))
    return len(due)


async def enqueue_due_polls(session: AsyncSession, limit: int = 200) -> int:
    from app.engine import queue

    now = datetime.now(UTC)
    due = (
        (
            await session.execute(
                select(WorkflowTrigger)
                .where(
                    WorkflowTrigger.enabled.is_(True),
                    WorkflowTrigger.trigger_type.in_(["email", "db_change", "file_uploaded"]),
                    WorkflowTrigger.next_run_at.is_not(None),
                    WorkflowTrigger.next_run_at <= now,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for trig in due:
        await queue.enqueue(
            session,
            executor.JOB_POLL,
            {"trigger_id": str(trig.id)},
            org_id=trig.org_id,
            dedupe_key=f"poll:{trig.id}",
            priority=120,
            max_attempts=3,
        )
        interval = int((trig.config or {}).get("poll_interval_seconds", 60))
        trig.next_run_at = now + timedelta(seconds=interval)
    return len(due)


# ----------------------------------------------------------------------------- polling


_POLL_SOURCES = {
    "email": ("new_email", {"folder", "from_filter", "subject_filter", "mark_seen"}),
    "db_change": ("new_rows", {"table", "cursor_column", "batch_size"}),
    "file_uploaded": ("new_file", {"folder_id"}),
}


async def poll_trigger(trigger_id: uuid.UUID) -> int:
    sm = get_sessionmaker()
    async with sm() as session:
        trig = (
            await session.execute(
                select(WorkflowTrigger).where(WorkflowTrigger.id == trigger_id).with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if trig is None or not trig.enabled or trig.trigger_type not in _POLL_SOURCES:
            return 0
        wf = await session.get(Workflow, trig.workflow_id)
        if wf is None or wf.status != "active":
            return 0
        cfg = trig.config or {}
        trigger_key, fields = _POLL_SOURCES[trig.trigger_type]
        interval = int(cfg.get("poll_interval_seconds", 60))
        started = 0
        try:
            resolved = await connection_service.resolve(
                session, trig.org_id, cfg.get("connection_id", ""), workspace_id=trig.workspace_id
            )
            if trigger_key not in resolved.connector.triggers:
                raise ConnectorError(f"Connector '{resolved.connector.key}' has no '{trigger_key}' trigger")
            result = await resolved.connector.poll(
                trigger_key, resolved.context, {k: v for k, v in cfg.items() if k in fields}, dict(trig.state or {})
            )
            items = result.items
            if trig.trigger_type == "db_change" and cfg.get("mode") == "batch" and items:
                batches: list[dict[str, Any]] = [{"rows": items}]
            elif trig.trigger_type == "db_change":
                batches = [{"row": i} for i in items]
            elif trig.trigger_type == "file_uploaded":
                batches = [{"file": i, "source": "google_drive"} for i in items]
            else:
                batches = items
            for payload in batches:
                key = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:40]
                _, created = await _start(session, trig, trig.trigger_type, payload, idempotency_key=f"poll:{key}")
                started += int(created)
            trig.state = result.state
            trig.last_error = None
            trig.next_run_at = datetime.now(UTC) + timedelta(seconds=interval)
        except (ConnectorError, LookupError) as exc:
            trig.last_error = getattr(exc, "message", str(exc))[:2000]
            trig.next_run_at = datetime.now(UTC) + timedelta(seconds=min(interval * 4, 3600))
        trig.last_run_at = datetime.now(UTC)
        await session.commit()
        return started
