"""Startup bootstrap: seed the template marketplace (idempotent)."""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.db.session import get_sessionmaker
from app.services.templates import seed_templates

logger = logging.getLogger(__name__)
_LOCK = 7_314_225_002


async def bootstrap() -> None:
    async with get_sessionmaker()() as session:
        # Serialise concurrent API replicas starting at the same time.
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK})
        count = await seed_templates(session)
        await session.commit()
    logger.info("bootstrap complete: %s templates", count)
