# Benchmarks

All numbers in this document come from `loadtest/flowforge_load.py` runs against the Docker Compose stack. The raw
JSON (including the per-second queue time series) and generated reports are in `loadtest/results/`. The harness
uses only the public API: it never reads the database. The scenario is described in
[TESTING.md](TESTING.md#load-testing).

## Test environment

| | |
|---|---|
| Host | **One** cloud VM: 4 vCPU (Intel Xeon), 16 GB RAM, Docker 29 |
| On that host | PostgreSQL 16, Redis 7, nginx, 2 API processes, 2 or 4 workers (32 concurrent jobs each), scheduler, sandbox (all mock APIs + LLM) **and the load generator** |
| Overlay | `loadtest/compose.loadtest.yml`: per-IP edge and login limits raised (all traffic comes from one IP); per-identity API limits unchanged |
| Workload | 1 org, 100 users (workflow developers), 500 published workflows: ⅓ pure logic (4–5 nodes), ⅓ HTTP enrichment → LLM classification → ERP write-back, ⅓ 3-way parallel fan-out + merge (7 nodes). Signed webhooks |

Every process shares the same 4 cores, so these are **floor** numbers for a single small machine. The
architecture scales by moving PostgreSQL to its own host and adding worker and API pods
([SCALING.md](SCALING.md)).

## Results: 2 API processes, 2 workers

<!-- RUN-2W -->

## Ingest capacity (workers stopped)

<!-- RUN-INGEST -->

## Scale-out: 2 API processes, 4 workers

<!-- RUN-4W -->

## Defects the load tests found (and fixed)

The first runs of the harness exposed problems that the unit and integration tests could not. Each is fixed, and
most now have a regression test.

| # | Symptom under load | Root cause | Fix | Regression test |
|---|---|---|---|---|
| 1 | A few executions stayed `RUNNING` forever with every node finished (28 of ~4,400 in one run) | **Lost wake-up race.** Enqueueing a deduplicated `execution.advance` used `ON CONFLICT DO NOTHING`. A worker could claim the already-queued advance *before* the enqueuing transaction committed, see the node still `RUNNING`, and finish; the skipped wake-up was gone | Coalescing enqueue became an upsert that row-locks the queued job until commit (claims use `SKIP LOCKED`). Plus a scheduler sweep that re-advances active executions with no pending work | `test_dedupe_enqueue_blocks_claim_until_commit`, `test_stalled_execution_sweep_recovers_lost_wakeup` |
| 2 | Under injected faults, 23% of executions failed even though nodes had 4 attempts | httpx timeouts and connection errors escaped the HTTP node as generic exceptions and were classified **non-retryable** | Transport errors and timeouts are retryable `ConnectorError`s; the runner also treats raw transport errors as retryable | `test_http_transport_failures_are_retryable`, `test_runner_classifies_raw_transport_errors_as_retryable` |
| 3 | Sporadic `404 Workflow not found` / `401 Session revoked` on the request right after a create or login | FastAPI ran the session dependency's commit *after* sending the response. With two API replicas, the next request could land on the other replica before the commit | Request transactions commit before the response (`Depends(..., scope="function")`) | `test_session_commits_before_response` |
| 4 | Trivial nodes took ~170 ms; the engine plateaued at ~104 node runs/s | Worker pool (20) smaller than job concurrency (32): overflow connections were opened and closed per transaction (TCP + auth + asyncpg type introspection), plus a liveness ping per checkout and a separate transaction to delete each finished job | Worker pool sized to concurrency + 8, no pre-ping on workers, jobs deleted in the same transaction as their state change | `test_jobs_complete_in_the_state_transaction` |
| 5 | Webhook ingest capped at ~66/s although the API processes were ~80% idle | `pg_notify` inside every enqueuing transaction: PostgreSQL takes a global lock at commit for transactions that issued NOTIFY, so **all** commits serialised (126 sessions waiting on `Lock:object`) | Wake-ups sent after commit, coalesced per process, from an autocommit connection | `test_wakeup_notification_is_sent_after_commit_only` |
| 6 | 1 in ~3,000 webhook POSTs returned 502 | uvicorn closed idle keep-alive connections after 5 s while nginx reused them for up to 60 s | uvicorn keep-alive 75 s, explicit nginx upstream keep-alive 60 s | measured: 0 errors in 6,166 webhooks |
| 7 | 502s from nginx after an API container was recreated | nginx resolved `api` once at start-up | Upstreams re-resolve through Docker DNS (`resolve`) | manual |

Fixing (1) also needed a compatibility fix: removing the never-implemented `concurrency_key` setting made
previously stored versions fail validation; the model now drops the legacy `null` key
(`test_stored_definitions_with_retired_null_setting_still_load`).

<!-- BEFORE-AFTER -->

## Reading the numbers

* **Steady state is the realistic number.** With 100 users each acting every 2–4 s, the system keeps up with no
  backlog. Webhook ingest p95 stays in the low hundreds of milliseconds, and executions finish in about a second
  end to end (p95), including HTTP calls and an LLM call for a third of them.
* **Burst numbers are a saturation test.** 3,000 webhooks arrive in about half a minute, far faster than the
  workers on a shared 4-core host can process them. The point is the *behaviour*: every request is accepted
  durably (no errors), the backlog shows up as queue depth and job age, it drains at a steady rate, and
  nothing is lost or stuck. End-to-end latency for the burst is dominated by time in the queue.
* **Chaos numbers show the retry machinery working.** About 22% of external calls fail or hang, hundreds of node
  retries are scheduled, and well under 1% of executions fail, only those where all 4 attempts of a call failed.
  With 20% per-attempt failure that is the expected rate (0.2⁴ ≈ 0.16% per call across ~2,500 calls). Every failed
  execution is visible in the dead-letter view, ready to requeue.
* **The bottleneck is the shared host.** PostgreSQL, the API, the workers and the load generator compete for 4
  cores. Moving PostgreSQL to its own instance and adding worker pods is the documented next step
  ([SCALING.md](SCALING.md)).
