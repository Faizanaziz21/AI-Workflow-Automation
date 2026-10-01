from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiomysql

from app.connectors.sdk import ConnectorError


@asynccontextmanager
async def connect(cfg: Any, creds: Any) -> AsyncIterator[aiomysql.Connection]:
    try:
        conn = await asyncio.wait_for(
            aiomysql.connect(
                host=cfg.host,
                port=cfg.port,
                db=cfg.database,
                user=creds.username,
                password=creds.password,
                ssl=ssl.create_default_context() if cfg.use_ssl else None,
                autocommit=False,
                charset="utf8mb4",
                connect_timeout=10,
            ),
            timeout=15,
        )
    except (TimeoutError, OSError, aiomysql.Error) as exc:
        raise ConnectorError(f"Could not connect to MySQL: {type(exc).__name__}: {exc}", retryable=True) from exc
    try:
        yield conn
    finally:
        conn.close()
