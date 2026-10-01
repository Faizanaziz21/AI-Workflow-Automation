# Deployment

FlowForge ships as three images: `backend` (api, worker, scheduler, migrations and CLI share one image),
`web` (Next.js standalone server) and the optional `sandbox`. It needs PostgreSQL 15+ and Redis 6+. Everything is
configured through environment variables.

## Local development (no containers)

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example .env            # adjust FF_DATABASE_URL / FF_REDIS_URL
alembic upgrade head
uvicorn app.main:app --reload --port 8000          # API on :8000 (docs at /docs)
python -m app.workers.worker                       # worker with embedded scheduler

# sandbox (fake HubSpot/Jira/Slack/SendGrid/LLM for demos)
cd ../mock-services && uvicorn sandbox.app:app --port 9000

# demo data: org, users, sandbox connections, all templates installed, sample executions
cd ../backend && python -m app.cli seed-demo --sandbox-url http://localhost:9000 --executions 30

# console
cd ../frontend && npm ci && FF_API_URL=http://localhost:8000 npm run dev    # http://localhost:3000
```

Demo login: `admin@acme.example` / `FlowForge-Demo-2026!`

## Docker Compose

```bash
cp .env.example .env                       # set real secrets for anything beyond a laptop
docker compose up -d --build               # core platform on http://localhost:8080
docker compose --profile demo up seed      # demo org, connections, templates, 40 sample runs
docker compose --profile tracing up -d     # + OpenTelemetry collector and Jaeger (http://localhost:16686)
```

| Service | Purpose | Exposed |
|---|---|---|
| `nginx` | Edge proxy: per-IP rate limits, routing `/api` → api, `/` → web, upstream DNS re-resolution | `:8080` |
| `api` ×2 | FastAPI (one uvicorn process per container) | internal `:8000` |
| `worker` ×2 | Job execution (`FF_EMBEDDED_SCHEDULER=false`) | metrics `:9101` |
| `scheduler` | Leader-elected maintenance loop (reaper, cron, polling, stalled sweep, gauges) | |
| `migrate` | `alembic upgrade head`, runs to completion before api/worker/scheduler start | |
| `web` | Next.js console | internal `:3000` |
| `postgres`, `redis` | State (named volumes `pgdata`, `redisdata`) | |
| `sandbox` | Mock SaaS + LLM for demos and load tests | internal `:9000` |
| `prometheus`, `grafana` | Metrics with alert rules; provisioned "FlowForge overview" dashboard | `:9090`, `:3001` |
| `seed` (profile `demo`) | One-off demo provisioning | |
| `otel-collector`, `jaeger` (profile `tracing`) | Trace pipeline | `:16686` |

Scale workers or API replicas without config changes: `docker compose up -d --scale worker=4 --scale api=3`.
nginx re-resolves the service names every 10 s.

For benchmarks, `loadtest/compose.loadtest.yml` raises the per-IP edge and login limits (all load comes from one
IP) and exposes the sandbox's chaos API. See [TESTING.md](TESTING.md#load-testing).

## Configuration

All settings use the `FF_` prefix (`backend/app/core/config.py`). List values accept CSV or JSON.

| Variable | Default | Notes |
|---|---|---|
| `FF_ENV` | `development` | `production` enables HSTS and refuses development secrets |
| `FF_DATABASE_URL` | `postgresql+asyncpg://flowforge:flowforge@localhost:5432/flowforge` | |
| `FF_REDIS_URL` | `redis://localhost:6379/0` | `rediss://` for TLS |
| `FF_JWT_SECRET` | dev value | ≥ 32 chars; **required in production** |
| `FF_ENCRYPTION_KEYS` | dev key | JSON map `{"v1": "<base64 32 bytes>", …}`; **required in production** |
| `FF_ACTIVE_ENCRYPTION_KEY` | `v1` | KEK version used for new secrets |
| `FF_PUBLIC_BASE_URL` | `http://localhost:8000` | Used in webhook URLs and OAuth redirects |
| `FF_CORS_ORIGINS` | `http://localhost:3000` | Only needed if the console is served from another origin |
| `FF_TRUSTED_PROXIES` | `1` | Number of proxies in front of the API (for client IPs in rate limits and audit) |
| `FF_RATE_LIMIT_DEFAULT` / `_AUTH` / `_WEBHOOK` | 600 / 20 / 3000 per minute | Per identity / per IP / per webhook token |
| `FF_WORKER_CONCURRENCY` | `32` | Concurrent jobs per worker process; the worker's DB pool is sized to this + 8 |
| `FF_DB_POOL_SIZE` / `FF_DB_MAX_OVERFLOW` | `30` / `20` | API process pool (keep the pool ≥ typical concurrency) |
| `FF_JOB_LEASE_SECONDS` | `60` | Lease before a crashed worker's job is reclaimed |
| `FF_DEFAULT_NODE_TIMEOUT_SECONDS` | `300` | |
| `FF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | 30 days | |
| `FF_MAX_NODE_OUTPUT_BYTES` | 1 MB | Larger outputs fail the node; use file storage |
| `FF_WEBHOOK_SYNC_TIMEOUT_SECONDS` | `30` | `wait_for_response` webhooks |
| `FF_ALLOW_PRIVATE_NETWORK_EGRESS` | `false` | `true` only for development / sandbox |
| `FF_EGRESS_ALLOWLIST` | empty | Host globs allowed despite private addresses (e.g. `*.corp.internal`) |
| `FF_STORAGE_DIR` | `./storage` | Uploaded files (mount a volume / PVC) |
| `FF_MAX_UPLOAD_BYTES` | 25 MB | |
| `FF_OTEL_EXPORTER_ENDPOINT` | unset | OTLP/HTTP endpoint enables tracing |
| `FF_LOG_LEVEL`, `FF_LOG_JSON` | `INFO`, `true` | |
| `FF_EMBEDDED_SCHEDULER` | `true` | Workers run the scheduler loop too; set `false` when running a dedicated scheduler |
| `FF_WORKER_METRICS_PORT` | `9101` | Worker Prometheus endpoint |

Generate secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"                        # FF_JWT_SECRET
python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"      # an encryption key
```

## Kubernetes

Manifests are plain Kustomize under `deploy/k8s`:

```
deploy/k8s/
├── base/
│   ├── namespace.yaml          flowforge namespace
│   ├── config.yaml             ConfigMap (non-secret settings) + Secret template (keys only)
│   ├── migrate-job.yaml        alembic upgrade head (run before rolling out a new version)
│   ├── api.yaml                Deployment, Service, HPA (CPU), PDB, PVC for uploads
│   ├── worker.yaml             Deployment, HPA (CPU + queue depth), PDB
│   ├── scheduler.yaml          Deployment (2 replicas; advisory-lock leader election)
│   ├── web.yaml                Deployment, Service
│   ├── ingress.yaml            TLS via cert-manager, body-size and rate-limit annotations
│   └── network-policies.yaml   default-deny; only required flows allowed
└── overlays/production/        image registry/tags, HPA floors
```

```bash
# 1. secrets (prefer External Secrets / Sealed Secrets; never commit values)
kubectl -n flowforge create secret generic flowforge-secrets \
  --from-literal=FF_DATABASE_URL=postgresql+asyncpg://… \
  --from-literal=FF_REDIS_URL=rediss://… \
  --from-literal=FF_JWT_SECRET=… \
  --from-literal=FF_ENCRYPTION_KEYS='{"v1":"…"}' \
  --from-literal=FF_ACTIVE_ENCRYPTION_KEY=v1

# 2. migrate, then roll out
kubectl apply -k deploy/k8s/overlays/production
kubectl -n flowforge wait --for=condition=complete job/flowforge-migrate --timeout=300s
kubectl -n flowforge rollout status deploy/api deploy/worker deploy/scheduler deploy/web
```

Notes:

* **PostgreSQL**: use a managed instance (RDS, Cloud SQL, Azure Flexible Server) with PITR backups. At more
  than a handful of worker pods, put **PgBouncer in transaction mode** in front of it. Each worker holds up to
  `FF_WORKER_CONCURRENCY + 8` connections. Workers also open one direct (non-pooled) LISTEN connection each,
  which must bypass PgBouncer. asyncpg uses prepared statements, so use PgBouncer ≥ 1.21 with
  `max_prepared_statements` enabled.
* **Redis**: any managed Redis. It holds only rate-limit state and synchronous webhook replies, so persistence
  is optional.
* **Uploads**: the API mounts a `ReadWriteMany` PVC at `/data/storage` that workers also mount. Use a storage
  class that supports RWX (EFS, Filestore, Azure Files).
* **Queue-depth autoscaling**: the worker HPA's external metric `flowforge_queue_depth{queue="ready"}` needs
  prometheus-adapter (or KEDA with a Prometheus scaler) exposing the scheduler's gauge. Without it, the HPA
  scales on CPU alone.
* **Security context**: all pods run as uid 10001 with a read-only root filesystem, no privilege escalation,
  all capabilities dropped and the RuntimeDefault seccomp profile.
* **Probes**: API liveness `/healthz` (process up), readiness `/readyz` (database and Redis reachable); worker
  liveness on its metrics port.
* **Graceful shutdown**: workers have `terminationGracePeriodSeconds` > their 30 s drain window. In-flight jobs
  finish or are released for other workers. PDBs keep at least one API and one worker pod during voluntary
  disruptions.

## Upgrades and migrations

* Migrations are forward-only Alembic revisions under `backend/migrations/versions`. CI fails if the models and
  migrations drift (`alembic check`).
* Order: run the migration job, then roll out api, worker, scheduler and web. Migrations are written to be
  backward compatible with the previous release (additive first, destructive changes in a later release), so a
  rolling update never runs old code against an incompatible schema.
* Published workflow versions are immutable. Executions continue on the version they started with across deploys.
* Rolling back the application is safe while the schema change is additive. Do not downgrade migrations in
  production.

## Backups and disaster recovery

* **PostgreSQL holds everything that matters**: definitions, executions, queue, approvals, audit, encrypted
  secrets. Use continuous archiving / PITR.
* **Encryption keys** (`FF_ENCRYPTION_KEYS`) must be backed up separately from database backups. Without them,
  stored credentials cannot be decrypted. Rotate with `rewrap-secrets` as described in [SECURITY.md](SECURITY.md).
* Uploaded files live in the storage volume; snapshot it alongside the database.
* After a restore, workers resume automatically: queued jobs are claimed, expired leases are reaped, and the
  stalled-execution sweep re-advances anything left without pending work.

## CI/CD

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | Steps |
|---|---|
| `backend` | Install, `ruff check` + `ruff format --check`, `alembic upgrade head && alembic check` against Postgres, `pytest` (PostgreSQL 16 + Redis 7 services) |
| `frontend` | `npm ci`, ESLint, `tsc --noEmit`, production build |
| `e2e` | Starts API, worker and sandbox, seeds the demo, builds the console and runs Playwright |
| `images` | Builds backend, web and sandbox images; pushes them to GHCR on `main` |

Deployments are pull-based: bump the image tags in the production overlay (or let Argo CD / Flux image automation
do it), and the GitOps controller applies the migration job and the rollout.

## Operations checklist

* Dashboards: Grafana "FlowForge — Platform overview" (executions/min, failure rate, ready and in-flight jobs,
  queue depth, job pickup latency p95, execution duration, node p95 by type, node retries and failures, AI
  calls/tokens/cost, HTTP p95 by route, webhooks received).
* Alerts (`deploy/prometheus/alerts.yml`): `HighExecutionFailureRate`, `QueueBacklog`, `QueueLatencyHigh`
  (job pickup p95; workers saturated or stuck), `AIErrorsElevated`, `APIHighLatency`.
* Runbooks:
  * *Oldest ready job rising*: scale workers or check for a slow dependency in node latency by type.
  * *Dead letters*: inspect in the console (Executions → Dead letters), fix the cause, then requeue.
  * *Webhook 429s*: raise `FF_RATE_LIMIT_WEBHOOK` or the ingress limit for that integration.
  * *Key rotation*: add a new KEK, set it active, deploy, run `rewrap-secrets`, then remove the old KEK.
