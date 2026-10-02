from __future__ import annotations

import asyncio
import hashlib
import ssl
from typing import Any

import asyncpg

from app.connectors.sdk import ConnectorError

_pools: dict[str, asyncpg.Pool] = {}
_lock = asyncio.Lock()


def _ssl_arg(mode: str) -> Any:
    if mode == "disable":
        return False
    if mode == "verify-full":
        return ssl.create_default_context()
    if mode == "require":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return "prefer"


async def get_pool(cfg: Any, creds: Any) -> asyncpg.Pool:
    key = hashlib.sha256(
        f"{cfg.host}|{cfg.port}|{cfg.database}|{creds.username}|{creds.password}|{cfg.ssl_mode}".encode()
    ).hexdigest()
    async with _lock:
        pool = _pools.get(key)
        if pool is None or pool._closed:
            try:
                pool = await asyncpg.create_pool(
                    host=cfg.host,
                    port=cfg.port,
                    database=cfg.database,
                    user=creds.username,
                    password=creds.password,
                    ssl=_ssl_arg(cfg.ssl_mode),
                    min_size=0,
                    max_size=5,
                    timeout=10,
                    command_timeout=cfg.statement_timeout_ms / 1000 + 5,
                    server_settings={
                        "application_name": "flowforge",
                        "statement_timeout": str(cfg.statement_timeout_ms),
                    },
                )
            except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
                raise ConnectorError(
                    f"Could not connect to PostgreSQL: {type(exc).__name__}: {exc}",
                    retryable=isinstance(exc, (OSError, asyncio.TimeoutError)),
                ) from exc
            _pools[key] = pool
        return pool


async def close_pools() -> None:
    for pool in list(_pools.values()):
        await pool.close()
    _pools.clear()
