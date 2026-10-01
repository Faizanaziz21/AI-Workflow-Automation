"""Queue worker process.

Run with ``python -m app.workers.worker``. Horizontally scalable: start as many replicas as needed; jobs are
distributed with ``SKIP LOCKED`` and protected by leases + heartbeats. On SIGTERM the worker stops claiming,
waits for in-flight jobs (grace period), then releases unfinished jobs back to the queue without burning an
attempt. A SIGKILL'd worker's jobs are recovered by the scheduler's lease reaper.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.metrics import JOB_LATENCY, JOBS_PROCESSED, WORKER_INFLIGHT
from app.core.telemetry import configure_tracer_provider, get_tracer
from app.db.models import Execution
from app.db.session import get_sessionmaker
from app.engine import executor, queue
from app.engine.runner import run_node
from app.services.triggers import poll_trigger

logger = logging.getLogger("flowforge.worker")
tracer = get_tracer("flowforge.worker")


def asyncpg_dsn() -> str:
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


class Worker:
    def __init__(
        self,
        concurrency: int | None = None,
        worker_id: str | None = None,
        queues: list[str] | None = None,
        grace_seconds: float = 30.0,
    ) -> None:
        settings = get_settings()
        self.concurrency = concurrency or settings.worker_concurrency
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.queues = queues or [queue.DEFAULT_QUEUE]
        self.lease = settings.job_lease_seconds
        self.poll_interval = settings.job_poll_interval_seconds
        self.grace_seconds = grace_seconds
        self.inflight: dict[int, tuple[asyncio.Task[None], queue.ClaimedJob]] = {}
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self.processed = 0
        self.handlers: dict[str, Callable[[queue.ClaimedJob], Awaitable[None]]] = {
            executor.JOB_ADVANCE: self._advance,
            executor.JOB_NODE: self._node,
            executor.JOB_TIMEOUT: self._timeout,
            executor.JOB_POLL: self._poll,
        }

    # -- handlers -------------------------------------------------------------------------------

    async def _advance(self, job: queue.ClaimedJob) -> None:
        async with get_sessionmaker()() as session:
            await executor.advance(session, uuid.UUID(job.payload["execution_id"]))
            await session.commit()

    async def _node(self, job: queue.ClaimedJob) -> None:
        await run_node(job.payload, self.worker_id)

    async def _timeout(self, job: queue.ClaimedJob) -> None:
        async with get_sessionmaker()() as session:
            await executor.handle_timeout(session, uuid.UUID(job.payload["execution_id"]))
            await session.commit()

    async def _poll(self, job: queue.ClaimedJob) -> None:
        await poll_trigger(uuid.UUID(job.payload["trigger_id"]))

    # -- loop -----------------------------------------------------------------------------------

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    async def _listen(self) -> None:
        """LISTEN for enqueue notifications so idle workers react within milliseconds."""
        while not self._stopping.is_set():
            conn = None
            try:
                conn = await asyncpg.connect(asyncpg_dsn())
                await conn.add_listener(queue.NOTIFY_CHANNEL, lambda *_: self._wake.set())
                await self._stopping.wait()
            except (OSError, asyncpg.PostgresError):
                logger.warning("LISTEN connection lost; retrying", exc_info=True)
                await asyncio.sleep(2)
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()

    async def _heartbeat(self) -> None:
        interval = max(1.0, self.lease / 3)
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            ids = list(self.inflight)
            if not ids:
                continue
            try:
                async with get_sessionmaker()() as session:
                    await queue.heartbeat(session, ids, self.worker_id, self.lease)
                    await session.commit()
                    await self._cancel_cancelled(session)
            except Exception:
                logger.warning("heartbeat failed", exc_info=True)

    async def _cancel_cancelled(self, session: Any) -> None:
        """Cooperative cancellation: stop node handlers whose execution was cancelled."""
        node_jobs = {jid: j for jid, (_, j) in self.inflight.items() if j.kind == executor.JOB_NODE and j.execution_id}
        if not node_jobs:
            return
        exec_ids = {j.execution_id for j in node_jobs.values()}
        cancelled = set(
            (
                await session.execute(
                    select(Execution.id).where(Execution.id.in_(exec_ids), Execution.cancel_requested.is_(True))
                )
            )
            .scalars()
            .all()
        )
        for jid, job in node_jobs.items():
            if job.execution_id in cancelled and jid in self.inflight:
                self.inflight[jid][0].cancel()

    async def _execute(self, job: queue.ClaimedJob) -> None:
        handler = self.handlers.get(job.kind)
        JOB_LATENCY.labels(job.kind).observe(max(0.0, time.time() - job.available_at.timestamp()))
        try:
            if handler is None:
                raise RuntimeError(f"No handler for job kind {job.kind!r}")
            with tracer.start_as_current_span(f"job {job.kind}", attributes={"flowforge.job_id": job.id}):
                await handler(job)
            async with get_sessionmaker()() as session:
                await queue.complete(session, job.id, self.worker_id)
                await session.commit()
            JOBS_PROCESSED.labels(job.kind, "ok").inc()
        except asyncio.CancelledError:
            if self._stopping.is_set():
                raise
            # Cancelled because the execution was cancelled: drop the job.
            async with get_sessionmaker()() as session:
                await queue.complete(session, job.id, self.worker_id)
                await session.commit()
            JOBS_PROCESSED.labels(job.kind, "cancelled").inc()
        except Exception as exc:
            logger.exception("job %s (%s) failed", job.id, job.kind)
            retry_in = min(300.0, 2.0**job.attempts)
            try:
                async with get_sessionmaker()() as session:
                    dead = await queue.fail(
                        session, job, self.worker_id, f"{type(exc).__name__}: {exc}", retry_in=retry_in
                    )
                    await session.commit()
                JOBS_PROCESSED.labels(job.kind, "dead" if dead else "retry").inc()
            except Exception:
                logger.exception("could not record job failure; lease expiry will recover job %s", job.id)
        finally:
            self.processed += 1

    async def run(self, *, stop_when_idle: bool = False, idle_timeout: float = 0.5) -> None:
        listener = asyncio.create_task(self._listen())
        heartbeat = asyncio.create_task(self._heartbeat())
        logger.info("worker %s started (concurrency=%s)", self.worker_id, self.concurrency)
        idle_since: float | None = None
        try:
            while not self._stopping.is_set():
                free = self.concurrency - len(self.inflight)
                claimed: list[queue.ClaimedJob] = []
                if free > 0:
                    try:
                        async with get_sessionmaker()() as session:
                            claimed = await queue.claim(
                                session, self.worker_id, limit=free, lease_seconds=self.lease, queues=self.queues
                            )
                            await session.commit()
                    except Exception:
                        logger.warning("claim failed", exc_info=True)
                        await asyncio.sleep(1)
                for job in claimed:
                    task = asyncio.create_task(self._execute(job))
                    self.inflight[job.id] = (task, job)
                    task.add_done_callback(lambda _t, jid=job.id: self._done(jid))
                WORKER_INFLIGHT.set(len(self.inflight))
                if claimed:
                    idle_since = None
                    continue
                if stop_when_idle:
                    if not self.inflight:
                        idle_since = idle_since or time.monotonic()
                        if time.monotonic() - idle_since >= idle_timeout:
                            break
                    else:
                        idle_since = None
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.poll_interval if not stop_when_idle else 0.05
                    )
        finally:
            await self._shutdown()
            self._stopping.set()
            listener.cancel()
            heartbeat.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(listener, heartbeat, return_exceptions=True)

    def _done(self, job_id: int) -> None:
        self.inflight.pop(job_id, None)
        self._wake.set()

    async def _shutdown(self) -> None:
        if not self.inflight:
            return
        logger.info("waiting up to %ss for %s in-flight jobs", self.grace_seconds, len(self.inflight))
        tasks = [t for t, _ in self.inflight.values()]
        _, pending = await asyncio.wait(tasks, timeout=self.grace_seconds)
        if pending:
            ids = [jid for jid, (t, _) in self.inflight.items() if t in pending]
            for t in pending:
                t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(*pending, return_exceptions=True)
            async with get_sessionmaker()() as session:
                await queue.release(session, ids, self.worker_id)
                await session.commit()
            logger.info("released %s unfinished jobs", len(ids))


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    configure_tracer_provider("-worker")
    if settings.metrics_enabled:
        from prometheus_client import start_http_server

        start_http_server(int(os.environ.get("FF_WORKER_METRICS_PORT", "9101")))
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)
    embedded = os.environ.get("FF_EMBEDDED_SCHEDULER", "true").lower() == "true"
    tasks = [asyncio.create_task(worker.run())]
    if embedded:
        from app.workers.scheduler import Scheduler

        scheduler = Scheduler()
        loop.add_signal_handler(signal.SIGTERM, lambda: (worker.stop(), scheduler.stop()))
        loop.add_signal_handler(signal.SIGINT, lambda: (worker.stop(), scheduler.stop()))
        tasks.append(asyncio.create_task(scheduler.run()))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
