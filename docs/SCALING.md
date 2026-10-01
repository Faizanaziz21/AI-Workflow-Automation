# Scaling

FlowForge scales by adding stateless processes in front of one PostgreSQL database. This page explains what each
tier does under load, what limits it, and how to grow. The numbers come from the recorded load tests in
[BENCHMARKS.md](BENCHMARKS.md).

## Tiers and how they scale

| Tier | State | Scales by | Limit you hit first |
|---|---|---|---|
| API (`uvicorn`, one process per pod) | none | Replicas behind the ingress (HPA on CPU) | PostgreSQL connections and commit rate; per-identity rate limits are shared through Redis |
| Workers | none (jobs leased in PostgreSQL) | Replicas (HPA on CPU and ready-queue depth) and `FF_WORKER_CONCURRENCY` | Round trips per node to PostgreSQL; then slow external APIs, which only occupy async slots |
| Scheduler | none (advisory-lock leader) | 2 replicas for availability; one is active | Never the bottleneck: it does periodic, indexed, bounded batches |
| PostgreSQL | everything | Vertical; PgBouncer; read replicas for dashboards; partitioning for history | Commit throughput (WAL fsync) and CPU |
| Redis | rate-limit buckets, sync-webhook replies | Vertical / managed | Not a bottleneck: O(1) operations per request |

**Waiting is free.** A workflow waiting three days for an approval, a timer or a customer reply is a row plus a
future-dated job, not a sleeping thread. Long-running automations therefore do not consume worker capacity, and
the number of in-flight executions is bounded by storage, not by workers.

**Slow dependencies only cost slots.** Node handlers are async. A worker with `FF_WORKER_CONCURRENCY=32` keeps
32 jobs in flight, so a 2-second LLM call blocks one slot, not a process. The chaos test injects 0.3–1 s latency
and 20-second hangs into every external call while throughput stays within the same order of magnitude.

## Where the time goes per node

A node execution costs about a dozen short PostgreSQL round trips across three small transactions (claim,
read context, persist + enqueue next advance + delete job), plus the advance that schedules it. Design choices
that keep this cheap:

* **One transaction per state change.** State, follow-up job and job completion commit together; there is no
  separate "ack".
* **Coalesced advances.** Fan-in of N branches produces about one advance, not N (dedupe index + upsert).
* **Post-commit, coalesced wake-ups.** `NOTIFY` is sent after commit, batched per process, from an autocommit
  connection. Issuing it inside transactions serialised all commits on PostgreSQL's notify lock (126 sessions
  waiting during a webhook burst before the fix).
* **Pools sized to concurrency.** An undersized SQLAlchemy pool turns every transaction into a new connection
  (TCP + auth + asyncpg type introspection). Workers size their pool to `FF_WORKER_CONCURRENCY + 8` and skip the
  per-checkout liveness ping.
* **Cached definitions.** Published versions are immutable, so parsed definitions and graphs are cached per
  process.
* **Small hot tables.** Finished jobs are deleted; history lives in `node_runs`/`execution_events`. The `jobs`
  table and its partial indexes stay small regardless of total volume.

## Measured on one 4-vCPU host

The recorded runs put **everything on one 4-vCPU VM**: PostgreSQL, Redis, nginx, 2 API processes, 2–4 workers,
the scheduler, the sandbox *and* the load generator. Every component competes for the same 4 cores, so treat the
numbers as a floor for a single small machine, not a ceiling for the architecture.

See [BENCHMARKS.md](BENCHMARKS.md) for the full tables. Headline figures:

<!-- BENCHMARK-SUMMARY -->

## Growing beyond one machine

1. **Separate the database.** Run PostgreSQL on its own instance with fast storage (commit latency is the main
   lever). Managed Postgres with provisioned IOPS works well.
2. **Add worker pods.** Throughput scales with workers until PostgreSQL CPU or commit rate saturates. The HPA uses
   the scheduler's `flowforge_queue_depth{queue="ready"}` gauge, so bursts add workers before latency builds up.
3. **Add PgBouncer (transaction mode)** once the connection count approaches `max_connections`
   (`api_pods × (FF_DB_POOL_SIZE + FF_DB_MAX_OVERFLOW) + worker_pods × (FF_WORKER_CONCURRENCY + 8)`). Each worker's LISTEN connection must bypass PgBouncer,
   and because asyncpg uses prepared statements, use PgBouncer ≥ 1.21 with `max_prepared_statements` enabled.
4. **Read replicas for dashboards.** Dashboard and audit queries are read-only and time-bounded; point them at a
   replica when they compete with the write path.
5. **Partition history.** `execution_events` and `node_runs` grow fastest. Partition them by month on
   `created_at`/`scheduled_at` and drop or archive old partitions according to your retention policy. The queue
   and the execution state machine only touch recent rows.
6. **Tune per workload.** Raise `FF_WORKER_CONCURRENCY` for I/O-heavy workflows (slow APIs, LLMs); lower it for
   CPU-heavy ones (large CSV/Excel transforms). Use `settings.max_parallel_nodes` to stop one wide workflow
   monopolising a worker.

## Back-pressure and fairness

* **Ingest never blocks on processing.** Webhooks and API runs return `202` as soon as the execution row and its
  first job are committed. A backlog shows up as queue depth and job age, not as failed requests.
* **Ordering**: jobs are claimed by `priority`, then `available_at` (oldest first). Resumptions after approvals
  and signals (40) and advances (50) go ahead of node runs (100); trigger polls (120) go last. An execution's own
  `priority` shifts its jobs forward, so urgent work can overtake a backlog.
* **Rate limits**: per identity and per webhook token, enforced in Redis across all API replicas, plus per-IP edge
  limits.
* **Alerting**: `QueueLatencyHigh` (job pickup p95) and `QueueBacklog` fire before users notice.
