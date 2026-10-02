# Testing

| Layer | Tooling | Count | Runs in CI |
|---|---|---|---|
| Backend unit, integration, API, engine, recovery, isolation, permission, connector, AI | pytest + pytest-asyncio, httpx ASGI client, respx, real PostgreSQL and Redis | 173 test cases | yes |
| End-to-end flagship workflow | pytest against the sandbox services (in-process) | 1 scenario, 23 nodes | yes |
| Console E2E | Playwright (Chromium) against a running stack | 3 journeys | yes |
| Static checks | ruff (lint + format), `alembic check`, ESLint, `tsc --noEmit` | | yes |
| Load and resilience | `loadtest/flowforge_load.py` against Docker Compose | 4 phases | on demand |

## Running the backend suite

```bash
cd backend
pip install -e ".[dev]"
createdb flowforge_test                      # PostgreSQL 15+ on localhost, user/password flowforge
redis-server --daemonize yes                 # Redis on localhost:6379
pytest -q                                    # ~40 s
pytest tests/test_engine.py -k retry -q      # a subset
```

The tests use real PostgreSQL and Redis rather than mocks, because the engine's correctness depends on
PostgreSQL semantics (`SKIP LOCKED`, partial unique indexes, row locks, `LISTEN/NOTIFY`). The schema is created
once per session, and tests isolate their data in their own tenants. External HTTP is mocked with `respx`. The flagship and polling
tests route connector traffic to the in-process sandbox app.

Helpers in `tests/conftest.py`:

* `tenant` / `make_tenant`: registered organization with an admin token and default workspace;
  `add_user(client, tenant, role, workspace_id=None)` adds a user with a given role and scope.
* `publish(client, tenant, definition)`, `run(...)`, `get_execution(...)`, `node_runs(...)`;
* `drain()`: runs an in-process worker until the queue is idle;
* `fast_forward(seconds)`: moves timers and backoff forward, so waits measured in days run instantly.

## What the suite covers

| File | Focus |
|---|---|
| `test_auth.py` | Registration, login, lockout, weak passwords, refresh rotation and reuse detection, cookie refresh with CSRF, logout, API keys, security headers, rate limiting, operator vs admin permissions, commit-before-response |
| `test_tenancy.py` | Workspace isolation, workspace-scoped roles, grant rules, last-admin protection, cross-tenant user access, teams, audit search and tenant scoping, dead letters and signals limited to permitted workspaces |
| `test_core_security.py` | Envelope encryption and AAD binding, KEK rotation and re-wrap, masking by key, value and pattern, webhook signatures, RBAC matrix, settings parsing |
| `test_connections_api.py` | Credentials encrypted and never returned, rotation keeps masked fields, invalid credentials not echoed, isolation and permissions, access policies, connection test endpoint, connector catalog |
| `test_connectors.py` | Request mapping and error classification for Slack, HubSpot, Jira, REST (OAuth client credentials), Google Drive token refresh, Twilio, Teams, email; SQL parameter safety and a real PostgreSQL round trip; SSRF egress policy; retryable transport errors |
| `test_expressions.py` | Evaluation semantics, sandbox escapes rejected, type-preserving rendering, operation budget, reference extraction |
| `test_validation.py` | Single trigger, unknown types and bad config, expressions skip static type checks, cycles, handles (incl. dynamic switch handles), loop-body isolation, expression references, cron semantics, approvers required, legacy settings compatibility |
| `test_workflows_api.py` | Publish/versions/diff/rollback lifecycle, publish blocked by validation errors, optimistic concurrency, clone and archive, webhook activation, RBAC, foreign connections rejected, node catalog |
| `test_engine.py` | Branching and dead-path elimination, switch, loops (scoped iterations), retries with backoff, dead letters and partial retry, `on_error`, node and execution timeouts, durable delays, event waits, cancel/pause/resume, **crash recovery via lease expiry**, poison jobs, idempotent runs, replay, sub-workflows, isolation, atomic job completion, **lost wake-up regression**, post-commit notifications, stalled-execution sweep |
| `test_approvals.py` | Refund over threshold requires approval, rejection path and quorum, separation of duties, escalation then expiry, request-info forms, cancelling an execution cancels its pending approval |
| `test_triggers.py` | Signed webhooks (bad/tampered signatures rejected) and dedupe, synchronous webhook responses, API events with filters, file upload trigger with CSV processing, cron exactly-once, database CDC polling, App trigger polling and error reporting |
| `test_ai.py` | Schema repair loop, retries then fallback with cost accounting, non-retryable errors, pricing, Anthropic structured output and refusal handling, Gemini request shape, AI nodes in workflows, prompt versioning and pinning |
| `test_templates.py` | Ten templates with unique slugs, every template valid once connections are mapped, installs without connections require selection |
| `test_retention.py` | Expired executions purged with their history, recent ones and unresolved failures kept, per-org override and validation, audit-log retention |
| `test_dashboard.py` | KPIs against known data, workspace scoping, Prometheus metrics endpoint |
| `test_flagship_e2e.py` | The Enterprise Customer Support Automation template end to end against the sandbox: AI triage, CRM lookup, KB-grounded reply, confidence gate, human review, ticket, Slack, email, analytics row |

Several of these began as defects found by the load harness and are kept as regression tests:
`test_dedupe_enqueue_blocks_claim_until_commit`, `test_wakeup_notification_is_sent_after_commit_only`,
`test_http_transport_failures_are_retryable`, `test_session_commits_before_response` and
`test_stalled_execution_sweep_recovers_lost_wakeup`.

## Console E2E (Playwright)

```bash
# with the stack running and seeded (docker compose up + --profile demo up seed)
cd frontend
E2E_BASE_URL=http://localhost:8080 npx playwright test
E2E_SCREENSHOTS=../docs/screenshots npx playwright test   # also refreshes the README screenshots
```

Journeys:

1. Log in, then visit the dashboard, the editor (flagship workflow, 23 nodes, node configuration and validation),
   the executions list, the execution inspector and timeline, approvals, templates, connections, AI usage and
   the audit log.
2. Approve a pending AI-drafted support reply and watch the execution resume.
3. A workflow fails, lands in dead letters, and the operator requeues it from the console. The retried run
   completes.

## Load testing

`loadtest/flowforge_load.py` drives a running deployment through its public API only, in four phases:

| Phase | What it does | Measures |
|---|---|---|
| setup | Registers an isolated org; creates 100 users (workflow developers), logs each in; users author and publish 500 workflows in three shapes (pure logic; HTTP API + LLM classification + write-back; 3-way parallel fan-out) with signed webhooks | Authoring API throughput |
| steady | 100 concurrent virtual users for 2 minutes: signed webhooks (55%), execution list/inspect, dashboard, workflow catalog, with 2–4 s think time | API latency per endpoint, engine throughput, end-to-end latency |
| burst | Thousands of simultaneous webhooks (250 in flight) across all 500 workflows | Ingest rate and latency, queue depth over time, drain time |
| chaos | The sandbox injects +300–1,000 ms latency, 15% HTTP 503 and 5% hung requests (20 s) on every API **and LLM** call while 1,000 webhooks arrive | Retries, failure rate after retries, end-to-end latency under faults |

```bash
docker compose -f docker-compose.yml -f loadtest/compose.loadtest.yml up -d
pip install httpx
python loadtest/flowforge_load.py --label my-run          # defaults: 100 users, 500 workflows
python loadtest/flowforge_load.py --help                  # tune users, workflows, burst size, fault rates
```

A single generator process tops out around 70–90 signed requests/s, so measure raw ingest capacity with several
in parallel: `docker compose stop worker && loadtest/parallel_ingest.sh 3 1500`.

Each run writes `loadtest/results/<timestamp>-<label>.json` (raw numbers, including the queue time series) and a
Markdown report. The recorded runs and their interpretation are in [BENCHMARKS.md](BENCHMARKS.md).
