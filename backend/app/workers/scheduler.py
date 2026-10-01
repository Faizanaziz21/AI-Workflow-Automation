"""Scheduler / maintenance loop (leader-elected).

Any number of replicas may run; a PostgreSQL advisory lock elects one leader that:

* reaps expired job leases (recovering work from crashed workers),
* fires due cron triggers exactly once per tick (row locks + idempotency keys),
* enqueues due connector-polling triggers (IMAP, database CDC, Google Drive, App triggers),
* re-advances stalled executions (active, but with no pending work) as a self-healing safety net,
* publishes queue-depth gauges.

It can run embedded in a worker (default) or standalone: ``python -m app.workers.scheduler``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import asyncpg

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.metrics import QUEUE_DEPTH
from app.db.session import get_sessionmaker
from app.engine import executor, queue
from app.services.triggers import enqueue_due_polls, fire_due_schedules
from app.workers.worker import asyncpg_dsn

logger = logging.getLogger("flowforge.scheduler")
LEADER_LOCK_ID = 7_314_225_001


class Scheduler:
    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._stopping = asyncio.Event()
        self.is_leader = False
        self._ticks = 0

    def stop(self) -> None:
        self._stopping.set()

    async def tick(self) -> dict[str, int]:
        sm = get_sessionmaker()
        stats: dict[str, int] = {}
        async with sm() as session:
            stats["reaped"] = await queue.reap_expired(session)
            await session.commit()
        async with sm() as session:
            stats["schedules"] = await fire_due_schedules(session)
            await session.commit()
        async with sm() as session:
            stats["polls"] = await enqueue_due_polls(session)
            await session.commit()
        self._ticks += 1
        if self._ticks % 30 == 1:  # roughly every 30 s
            async with sm() as session:
                stats["stalled"] = await executor.recover_stalled(session)
                await session.commit()
            if stats["stalled"]:
                logger.warning("re-advanced %s stalled executions", stats["stalled"])
        if stats["reaped"]:
            logger.warning("recovered %s jobs with expired leases", stats["reaped"])
        return stats

    async def _update_gauges(self) -> None:
        async with get_sessionmaker()() as session:
            d = await queue.depth(session)
        QUEUE_DEPTH.labels("ready").set(d["ready"])
        QUEUE_DEPTH.labels("delayed").set(d["delayed"])

    async def run(self) -> None:
        conn: asyncpg.Connection | None = None
        ticks = 0
        while not self._stopping.is_set():
            try:
                if conn is None or conn.is_closed():
                    conn = await asyncpg.connect(asyncpg_dsn())
                    self.is_leader = False
                if not self.is_leader:
                    self.is_leader = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_LOCK_ID))
                    if self.is_leader:
                        logger.info("scheduler acquired leadership")
                if self.is_leader:
                    await self.tick()
                    ticks += 1
                    if ticks % 5 == 0:
                        await self._update_gauges()
            except Exception:
                logger.exception("scheduler tick failed")
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()
                conn = None
                self.is_leader = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval)
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    scheduler = Scheduler()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, scheduler.stop)
    await scheduler.run()


if __name__ == "__main__":
    asyncio.run(main())
