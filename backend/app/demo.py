"""Demo environment provisioning: sandbox connections, analytics tables, flagship workflow.

Used by ``python -m app.cli seed-demo`` and by the end-to-end test-suite. All connections point at the
FlowForge Sandbox (``mock-services``) through the *standard* connectors; swap the base URLs/credentials for
real HubSpot/Jira/Slack/SendGrid/OpenAI accounts and nothing else changes.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.principal import Principal
from app.services import connections as connection_service

ANALYTICS_DDL = [
    """CREATE TABLE IF NOT EXISTS support_analytics (
        id bigserial PRIMARY KEY, execution_id text, customer_email text, category text, sentiment text,
        priority text, tier text, auto_sent boolean, root_cause text, ai_confidence numeric, created_at timestamptz)""",
    """CREATE TABLE IF NOT EXISTS lead_analytics (
        id bigserial PRIMARY KEY, email text, company text, score int, segment text, replied boolean, created_at timestamptz)""",
]


def database_parts() -> dict[str, Any]:
    url = urlparse(get_settings().database_url.replace("+asyncpg", ""))
    return {
        "host": url.hostname or "localhost",
        "port": url.port or 5432,
        "database": url.path.lstrip("/"),
        "username": url.username or "flowforge",
        "password": url.password or "",
    }


async def provision_demo_connections(
    session: AsyncSession,
    principal: Principal,
    workspace_id: uuid.UUID,
    sandbox_url: str,
) -> dict[str, str]:
    base = sandbox_url.rstrip("/")
    db = database_parts()
    specs: dict[str, tuple[str, str, dict[str, Any], dict[str, Any]]] = {
        "ai": (
            "Sandbox LLM (OpenAI-compatible)",
            "openai_compatible",
            {
                "base_url": f"{base}/v1",
                "default_model": "sandbox-llm",
                "default_embedding_model": "sandbox-embed",
                "json_mode": "json_object",
            },
            {},
        ),
        "crm": (
            "CRM (HubSpot API)",
            "hubspot",
            {"api_base_url": base, "portal_id": "424242"},
            {"access_token": "pat-sandbox-token"},
        ),
        "helpdesk": (
            "Helpdesk API",
            "http_rest",
            {"base_url": f"{base}/helpdesk", "auth_mode": "bearer", "health_path": "/"},
            {"token": "helpdesk-sandbox-token"},
        ),
        "email": (
            "Transactional email (SendGrid API)",
            "sendgrid",
            {"api_base_url": base, "from_address": "support@acme.example", "from_name": "Acme Support"},
            {"api_key": "SG.sandbox-key"},
        ),
        "slack": (
            "Slack workspace",
            "slack",
            {"api_base_url": f"{base}/api", "default_channel": "#support"},
            {"bot_token": "xoxb-sandbox-token"},
        ),
        "tickets": (
            "Jira",
            "jira",
            {"site_url": base, "default_project": "SUP"},
            {"email": "bot@acme.example", "api_token": "jira-sandbox-token"},
        ),
        "analytics_db": (
            "Analytics warehouse",
            "postgres",
            {"host": db["host"], "port": db["port"], "database": db["database"], "ssl_mode": "disable"},
            {"username": db["username"], "password": db["password"]},
        ),
        "enrichment": ("Company enrichment API", "http_rest", {"base_url": f"{base}/enrichment"}, {}),
        "erp": ("ERP API", "http_rest", {"base_url": f"{base}/erp"}, {}),
    }
    existing = {
        c.name: c
        for c in (
            await session.execute(text("SELECT id, name FROM connections WHERE workspace_id = :w"), {"w": workspace_id})
        ).all()
    }
    out: dict[str, str] = {}
    for role, (name, key, config, creds) in specs.items():
        if name in existing:
            out[role] = str(existing[name].id)
            continue
        conn = await connection_service.create_connection(
            session,
            principal,
            workspace_id=workspace_id,
            name=name,
            connector_key=key,
            config=config,
            credentials=creds,
            access_policy={},
        )
        out[role] = str(conn.id)
    for ddl in ANALYTICS_DDL:
        await session.execute(text(ddl))
    return out
