"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_pool_size: int | None = None
_pre_ping = True


def configure_pool(size: int, *, pre_ping: bool = True) -> None:
    """Set the connection-pool size before the engine is first used.

    Workers call this with their job concurrency: when more sessions are checked out than the pool holds,
    SQLAlchemy opens *overflow* connections and closes them again on return, so an undersized pool turns into a
    connect + type-introspection round trip on almost every transaction under load.

    ``pre_ping`` adds a liveness round trip to every checkout. Workers disable it: a job that hits a dead
    connection fails, SQLAlchemy invalidates the connection, and the job is re-queued, so the ping only adds
    latency to the hot path.
    """
    global _pool_size, _pre_ping
    if _engine is not None:
        raise RuntimeError("configure_pool() must be called before the engine is created")
    _pool_size = size
    _pre_ping = pre_ping


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=max(settings.db_pool_size, _pool_size or 0),
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=_pre_ping,
            pool_recycle=1800,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commits on success, rolls back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
