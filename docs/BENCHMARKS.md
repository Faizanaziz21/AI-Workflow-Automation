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

### Execution engine

| Phase | Executions | Completed | Failed | Failure rate | Throughput | E2E p50 | E2E p95 | Node retries | Drain after load |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| steady | 2,166 | 2,166 | 0 | 0.00% | 18.0/s | 287 ms | 1,134 ms | 0 | 0.0 s |
| burst | 3,000 | 3,000 | 0 | 0.00% | 31.7/s | 61,665 ms | 75,546 ms | 0 | 60.7 s |
| chaos | 1,000 | 994 | 6 | 0.60% | 11.3/s | 57,335 ms | 65,636 ms | 704 | 77.2 s |

### API latency

| Phase | Endpoint | Requests | RPS | p50 | p95 | p99 | Errors |
|---|---|---:|---:|---:|---:|---:|---:|
| steady | `GET /dashboard/summary` | 344 | 2.8 | 189.9 ms | 1019.4 ms | 1465.8 ms | 0.00% |
| steady | `GET /executions` | 565 | 4.6 | 37.1 ms | 141.1 ms | 573.9 ms | 0.00% |
| steady | `GET /executions/{id}` | 382 | 3.1 | 24.0 ms | 76.7 ms | 406.4 ms | 0.00% |
| steady | `GET /executions/{id}/nodes` | 382 | 3.1 | 21.6 ms | 68.2 ms | 145.6 ms | 0.00% |
| steady | `GET /workflows` | 312 | 2.5 | 106.0 ms | 368.1 ms | 949.5 ms | 0.00% |
| steady | `GET /workflows/{id}` | 145 | 1.2 | 24.3 ms | 113.5 ms | 686.1 ms | 0.00% |
| steady | `POST /hooks/{token}` | 2,166 | 17.5 | 31.6 ms | 159.4 ms | 717.2 ms | 0.00% |
| burst | `POST /hooks/{token} (burst)` | 3,000 | 87.9 | 2732.7 ms | 4837.2 ms | 5723.8 ms | 0.00% |
| chaos | `POST /hooks/{token} (chaos)` | 1,000 | 80.1 | 2676.1 ms | 5493.1 ms | 6916.0 ms | 0.00% |

### Queue behaviour

| Phase | Max ready | Max running | Max delayed (timers + retry backoff) | Oldest ready job |
|---|---:|---:|---:|---:|
| steady | 51 | 43 | 73 | 0.28 s |
| burst | 3,744 | 63 | 2,916 | 24.28 s |
| chaos | 1,512 | 64 | 992 | 25.41 s |

**burst:** 3,000 webhooks to 500 workflows, 3,000 accepted in 34.12 s (87.9/s ingest).

**chaos:** 1,000 webhooks to 333 workflows, 1,000 accepted in 12.48 s (80.1/s ingest).

Chaos faults on every sandbox call (HTTP APIs and the LLM): +300 ms (+0–700 ms jitter), 15% HTTP 503, 5% hung for 20 s.

## Ingest capacity (workers stopped)

With workers stopped, 3,000 signed webhooks (250 in flight) were all accepted (0 errors) in 42.3 s:
**70.9 webhooks/s** across two API processes, p50 2,244 ms, p95 8,494 ms. Each accepted
webhook is a committed execution plus its first job, so a 202 means the work is durable.

At this concurrency the latency is queueing (250 in flight ÷ ~71/s ≈ 3.5 s). This figure is close to the 65.7/s
measured before the `NOTIFY` fix, so with workers stopped another limit dominates. The likeliest candidate is the
number of sequential database round trips per webhook: trigger lookup, signing-secret decrypt, idempotency check,
then the execution, trigger node run, event and two jobs. Batching that path is the next optimisation to profile.
In steady state, webhook p95 is ~160 ms.

## Scale-out: 2 API processes, 4 workers

### Execution engine

| Phase | Executions | Completed | Failed | Failure rate | Throughput | E2E p50 | E2E p95 | Node retries | Drain after load |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| steady | 2,099 | 2,099 | 0 | 0.00% | 17.5/s | 323 ms | 719 ms | 0 | 0.0 s |
| burst | 3,000 | 3,000 | 0 | 0.00% | 34.5/s | 43,200 ms | 56,660 ms | 0 | 37.2 s |
| chaos | 1,000 | 998 | 2 | 0.20% | 18.0/s | 29,698 ms | 37,885 ms | 664 | 39.2 s |

### API latency

| Phase | Endpoint | Requests | RPS | p50 | p95 | p99 | Errors |
|---|---|---:|---:|---:|---:|---:|---:|
| steady | `GET /dashboard/summary` | 347 | 2.8 | 109.5 ms | 369.4 ms | 820.0 ms | 0.00% |
| steady | `GET /executions` | 576 | 4.7 | 42.3 ms | 128.7 ms | 257.6 ms | 0.00% |
| steady | `GET /executions/{id}` | 417 | 3.4 | 29.2 ms | 98.7 ms | 293.0 ms | 0.00% |
| steady | `GET /executions/{id}/nodes` | 417 | 3.4 | 28.4 ms | 85.2 ms | 284.4 ms | 0.00% |
| steady | `GET /workflows` | 308 | 2.5 | 145.6 ms | 423.2 ms | 967.5 ms | 0.00% |
| steady | `GET /workflows/{id}` | 167 | 1.3 | 36.4 ms | 141.3 ms | 209.9 ms | 0.00% |
| steady | `POST /hooks/{token}` | 2,099 | 17.0 | 39.2 ms | 156.0 ms | 554.6 ms | 0.00% |
| burst | `POST /hooks/{token} (burst)` | 3,000 | 59.8 | 3522.2 ms | 10406.3 ms | 14682.8 ms | 0.00% |
| chaos | `POST /hooks/{token} (chaos)` | 1,000 | 58.3 | 3806.4 ms | 6171.6 ms | 7059.5 ms | 0.00% |

### Queue behaviour

| Phase | Max ready | Max running | Max delayed (timers + retry backoff) | Oldest ready job |
|---|---:|---:|---:|---:|
| steady | 7 | 27 | 35 | 0.16 s |
| burst | 2,616 | 110 | 2,220 | 17.57 s |
| chaos | 1,219 | 126 | 997 | 10.47 s |

**burst:** 3,000 webhooks to 500 workflows, 3,000 accepted in 50.14 s (59.8/s ingest).

**chaos:** 1,000 webhooks to 333 workflows, 1,000 accepted in 17.16 s (58.3/s ingest).

Chaos faults on every sandbox call (HTTP APIs and the LLM): +300 ms (+0–700 ms jitter), 15% HTTP 503, 5% hung for 20 s.

Doubling the workers on the **same 4 cores** still helps where work waits on I/O. Burst drain time fell from 60.7 s
to 37.2 s and end-to-end p95 from 75.5 s to 56.7 s. Under chaos, throughput rose from 11.3 to 18.0 executions/s
and p95 fell from 65.6 s to 37.9 s, because more slots are available while calls hang. Ingest dropped (87.9 → 59.8
webhooks/s) because four workers now compete with the API for the same CPU and database. On real
infrastructure, workers and API run on separate nodes.

## Defects the load tests found (and fixed)

The first runs of the harness exposed problems that the unit and integration tests could not. Each is fixed, and
most now have a regression test.

| # | Symptom under load | Root cause | Fix | Regression test |
|---|---|---|---|---|
| 1 | A few executions stayed `RUNNING` forever with every node finished (28 of ~4,400 in one run) | **Lost wake-up race.** Enqueueing a deduplicated `execution.advance` used `ON CONFLICT DO NOTHING`. A worker could claim the already-queued advance *before* the enqueuing transaction committed, see the node still `RUNNING`, and finish; the skipped wake-up was gone | Coalescing enqueue became an upsert that row-locks the queued job until commit (claims use `SKIP LOCKED`). Plus a scheduler sweep that re-advances active executions with no pending work | `test_dedupe_enqueue_blocks_claim_until_commit`, `test_stalled_execution_sweep_recovers_lost_wakeup` |
| 2 | Under injected faults, 23% of executions failed even though nodes had 4 attempts | httpx timeouts and connection errors escaped the HTTP node as generic exceptions and were classified **non-retryable** | Transport errors and timeouts are retryable `ConnectorError`s; the runner also treats raw transport errors as retryable | `test_http_transport_failures_are_retryable`, `test_runner_classifies_raw_transport_errors_as_retryable` |
| 3 | Sporadic `404 Workflow not found` / `401 Session revoked` on the request right after a create or login | FastAPI ran the session dependency's commit *after* sending the response. With two API replicas, the next request could land on the other replica before the commit | Request transactions commit before the response (`Depends(..., scope="function")`) | `test_session_commits_before_response` |
| 4 | Trivial nodes took ~170 ms; the engine plateaued at ~104 node runs/s | Worker pool (20) smaller than job concurrency (32): overflow connections were opened and closed per transaction (TCP + auth + asyncpg type introspection), plus a liveness ping per checkout and a separate transaction to delete each finished job | Worker pool sized to concurrency + 8, no pre-ping on workers, jobs deleted in the same transaction as their state change | `test_jobs_complete_in_the_state_transaction` |
| 5 | During webhook bursts the API processes were ~80% idle while requests queued | `pg_notify` inside every enqueuing transaction: PostgreSQL takes a global lock at commit for transactions that issued NOTIFY, so **all** commits serialised (126 sessions waiting on `Lock:object`) | Wake-ups sent after commit, coalesced per process, from an autocommit connection. With workers running, burst ingest rose from 75 to 88 webhooks/s and its p95 fell from 7.2 s to 4.8 s | `test_wakeup_notification_is_sent_after_commit_only` |
| 6 | 1 in ~3,000 webhook POSTs returned 502 | uvicorn closed idle keep-alive connections after 5 s while nginx reused them for up to 60 s | uvicorn keep-alive 75 s, explicit nginx upstream keep-alive 60 s | measured: 0 errors in 6,166 webhooks |
| 7 | 502s from nginx after an API container was recreated | nginx resolved `api` once at start-up | Upstreams re-resolve through Docker DNS (`resolve`) | manual |

Fixing (1) also needed a compatibility fix: removing the never-implemented `concurrency_key` setting made
previously stored versions fail validation; the model now drops the legacy `null` key
(`test_stored_definitions_with_retired_null_setting_still_load`).

### Before and after the fixes

| Metric (same host, 2 API / 2 workers) | First runs | After fixes |
|---|---:|---:|
| Executions stuck in `RUNNING` | 28 in ~4,400 | 0 in 6,166 (plus the self-healing sweep) |
| Failure rate under injected faults | 23% (smoke run) | 0.6% |
| Webhook 5xx during bursts | 1 in 3,000 | 0 in 4,000 |
| Trivial node latency (set variable, claim → persisted) | ~170 ms avg | p50 19 ms, p95 55 ms (steady phase, 4-worker run) |
| Node runs finished per second, busiest minute of a burst | ~104 | 162 |

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
