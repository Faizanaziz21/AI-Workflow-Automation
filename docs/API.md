# REST API

Base path `/api/v1`. JSON in, JSON out. The live OpenAPI document is served at `/openapi.json`, with
interactive documentation at `/docs` (Swagger UI) and `/redoc`. This page covers the conventions and the
endpoints you need most.

## Conventions

**Authentication**

| Client | How |
|---|---|
| Browser console | `POST /auth/login` returns a short-lived access token (keep it in memory) and sets the refresh-token cookie; send `Authorization: Bearer <access>` |
| Server-to-server | `X-API-Key: ffk_<prefix>_<secret>` (created under Settings → API keys, scoped to a role and optionally a workspace) |
| Webhooks | No platform credentials: the trigger's HMAC signature, API key, or explicitly none (see below) |

**Tenancy.** The organization always comes from the credential, never from the URL. Workspace-scoped
resources live under `/workspaces/{workspace_id}/…`. Ids from other tenants, or from workspaces you cannot access,
return `404`.

**Errors** use one envelope:

```json
{"error": {"code": "validation_failed", "message": "Request validation failed",
           "details": [{"loc": ["body", "name"], "msg": "Field required", "type": "missing"}]}}
```

| Status | `code` examples |
|---|---|
| 400 | `bad_request` |
| 401 | `unauthenticated` (with `WWW-Authenticate: Bearer`) |
| 403 | `forbidden`, `csrf_failed` |
| 404 | `not_found` |
| 409 | `conflict`, `not_published` |
| 422 | `validation_failed` (request bodies and workflow definitions) |
| 429 | `rate_limited` (with `Retry-After` seconds) |

**Pagination.** List endpoints return `{"items": [...], "total": n, "limit": n, "next_cursor": "..."}`.
Executions page by cursor (`before=<next_cursor>`), audit logs by `before_id`; other collections use
`limit`/`offset`.

**Idempotency.** `POST …/run` and `POST /events` accept an `idempotency_key` field; webhooks honour the
`Idempotency-Key` header (or a header configured on the trigger, such as `X-GitHub-Delivery`). A repeat returns
the original execution.

**Masking.** Execution payloads, node inputs/outputs and errors are returned with secrets masked (`••••••••`).
Org Admins (`executions:read_sensitive`) see unmasked business data, but never credential values.

## Quick start

```bash
BASE=http://localhost:8080/api/v1
TOKEN=$(curl -s $BASE/auth/login -H 'content-type: application/json' \
  -d '{"email":"admin@acme.example","password":"FlowForge-Demo-2026!"}' | jq -r .access_token)
WS=$(curl -s $BASE/workspaces -H "authorization: Bearer $TOKEN" | jq -r '.[0].id')

# create, publish and run a workflow
WF=$(curl -s $BASE/workspaces/$WS/workflows -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{
  "name": "Hello",
  "definition": {
    "nodes": [{"id": "start", "type": "trigger.manual"},
              {"id": "greet", "type": "logic.set_variable",
               "config": {"assignments": {"greeting": "Hello {{ trigger.name }}"}}}],
    "edges": [{"source": "start", "target": "greet"}]
  }}' | jq -r .id)
curl -s -X POST $BASE/workspaces/$WS/workflows/$WF/publish -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{}'
EX=$(curl -s $BASE/workspaces/$WS/workflows/$WF/run -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"input": {"name": "Ada"}}' | jq -r .execution_id)
curl -s $BASE/executions/$EX -H "authorization: Bearer $TOKEN" | jq '{status, output}'
```

## Endpoints

### Auth

| Method & path | Description |
|---|---|
| `POST /auth/register-org` | Create an organization and its first Org Admin; returns tokens |
| `POST /auth/login` | Email + password → `{access_token, refresh_token, expires_in}` (+ refresh cookie) |
| `POST /auth/refresh` | Rotate the refresh token (body, or cookie + `X-CSRF-Token`) |
| `POST /auth/logout` | Revoke the session family |
| `GET /auth/me` | User, organization, org and workspace roles, effective permissions |

### Organization, workspaces, users, teams, API keys

| Method & path | Permission |
|---|---|
| `GET/PATCH /orgs/current` | read: any; patch: `org:manage` |
| `GET/POST /workspaces`, `PATCH/DELETE /workspaces/{id}` | `workspaces:manage` to change |
| `GET/POST /users`, `PATCH /users/{id}` | `users:read` / `users:manage` |
| `POST /users/{id}/roles`, `DELETE /users/{id}/roles/{assignment}` | `users:manage`, and only roles at or below your own |
| `GET/POST /teams`, `POST /teams/{id}/members`, `DELETE /teams/{id}` | `teams:manage` to change |
| `GET/POST /api-keys`, `DELETE /api-keys/{id}` | `api_keys:manage`; the secret is returned once |

### Catalog

| Method & path | Description |
|---|---|
| `GET /node-types` | All 76 node types: category, JSON schema of the config, output schema, handles, connection requirements |
| `GET /node-types/{type}` | One node type |
| `POST /node-types/handles` | Output handles for a node config (dynamic handles of switch, classify, router, decision) |
| `GET /connectors`, `GET /connectors/{key}` | Connector metadata, auth and config schemas, actions, triggers |
| `GET /templates`, `GET /templates/{slug}` | Template marketplace |
| `POST /templates/{slug}/install` | Install into a workspace, binding `connections: {role: connection_id}` |

### Workflows and versions

| Method & path | Description |
|---|---|
| `GET /workspaces/{ws}/workflows` | Filter by `status` (`active` default, `archived`, `all`), `q`, `tag` |
| `POST /workspaces/{ws}/workflows` | Create (optionally with a `definition`) |
| `GET/PATCH/DELETE /workspaces/{ws}/workflows/{id}` | Read, rename or retag, delete (`workflows:delete`) |
| `PUT /workspaces/{ws}/workflows/{id}/draft` | Save the draft definition (validated, never executed) |
| `POST /workspaces/{ws}/workflows/{id}/validate` | Validate a definition without saving: errors and warnings per node, field and edge |
| `POST /workspaces/{ws}/workflows/{id}/publish` | Snapshot the draft as an immutable version and arm its trigger |
| `GET …/versions`, `GET …/versions/{n}`, `GET …/versions/diff?from=1&to=3` | History and structural diff (nodes and edges added, removed or changed, with field-level changes) |
| `POST …/rollback` | Publish a copy of an earlier version |
| `POST …/clone`, `POST …/archive`, `POST …/restore` | Lifecycle |
| `GET …/trigger` | Trigger status; webhook URL; `?reveal_secret=true` reveals the signing secret (audited) |
| `POST …/trigger/rotate-secret` | New webhook signing secret |

### Executions

| Method & path | Description |
|---|---|
| `POST /workspaces/{ws}/workflows/{id}/run` | Start a run: `{input, idempotency_key?, correlation_key?}` → `202 {execution_id}` |
| `GET /executions` | Filter by `workspace_id`, `workflow_id`, `status` (repeatable), `trigger_type`, `correlation_key`, `since`, `until`; cursor `before` |
| `GET /executions/{id}` | Status, trigger payload, output, error, node-run summaries |
| `GET /executions/{id}/nodes` | Node runs with inputs, outputs, attempts, durations, branches |
| `GET /executions/{id}/events` | Timeline (node started, retried, completed, waits, approvals, signals, operator actions) |
| `POST /executions/{id}/cancel`, `/pause`, `/resume` | Operator controls (`executions:operate`) |
| `POST /executions/{id}/retry` | Partial retry: keep completed checkpoints, re-run what failed |
| `POST /executions/{id}/nodes/{node_id}/replay` | Re-run a node and its descendants |
| `POST /executions/{id}/signals`, `POST /signals` | Resume event waits by key, e.g. `{"key": "reply:jane@globex.com", "payload": {...}}` |
| `GET /dead-letters`, `POST /dead-letters/{id}/requeue`, `POST /dead-letters/{id}/resolve` | Dead-letter queue (scoped to your workspaces) |
| `GET /workspaces/{ws}/queue` | Queue depth: ready, delayed, running, oldest ready job age |

### Triggers

| Method & path | Description |
|---|---|
| `POST\|PUT\|GET /hooks/{token}` | Public webhook endpoint (see below) |
| `POST /events` | Publish an API event `{name, data, event_id?, workspace_id?}` to every `trigger.api_event` listening for it |
| `POST /workspaces/{ws}/files` | Upload a file (multipart); starts `trigger.file_uploaded` workflows |
| `GET /workspaces/{ws}/files`, `GET …/files/{id}/download` | Stored files |

### Approvals

| Method & path | Description |
|---|---|
| `GET /approvals` | Inbox of requests the caller can act on (assigned directly, via team or via role): `?status=pending` (default), `workspace_id` |
| `GET /approvals/{id}` | Request, context, history |
| `POST /approvals/{id}/approve`, `POST …/reject` | `{comment?, response_data?}`; `response_data` carries edited fields (manual review) or form answers (request-info) |
| `POST /approvals/{id}/comment` | Add a comment |
| `POST /approvals/{id}/reassign` | `{user_ids, team_ids, note}` |

### Connections

| Method & path | Description |
|---|---|
| `GET/POST /workspaces/{ws}/connections` | Credentials are write-only; responses list which fields are set |
| `GET/PATCH/DELETE /workspaces/{ws}/connections/{id}` | Update config, access policy, or credentials (masked placeholders are ignored) |
| `POST …/{id}/test` | Run the connector's connection test |
| `POST …/{id}/rotate` | Replace credentials (new secret version, audited) |
| `POST …/{id}/oauth/start` | OAuth 2.0 + PKCE: returns the provider's authorize URL; the callback completes the connection |
| `POST /admin/secrets/rewrap` | Re-wrap all data keys with the active KEK (Org Admin) |

### AI

| Method & path | Description |
|---|---|
| `GET /ai/prompts`, `GET /ai/prompts/{key}/versions` | Prompt library and versions |
| `POST /ai/prompts/{key}/versions` | Publish a prompt version |
| `POST /ai/prompts/{key}/versions/{v}/activate` / `deactivate` | Choose the active version |
| `GET /ai/pricing` | Effective model prices (defaults + org overrides) |
| `GET /ai/usage?days=30&group_by=model\|provider\|workflow\|day\|prompt` | Calls, tokens, cost, failure rate, latency |

### Dashboard, audit, health

| Method & path | Description |
|---|---|
| `GET /dashboard/summary?window=15m\|1h\|6h\|24h\|7d\|30d&workspace_id=` | KPIs: running, completed, failure rate, durations, throughput, queue, AI calls and cost, retries, approvals |
| `GET /dashboard/timeseries` | Executions by outcome per bucket, AI cost |
| `GET /dashboard/workflows`, `GET /dashboard/nodes` | Per-workflow and per-node-type stats (failure rate, p95, retries) |
| `GET /audit-logs?q=&action=workflow.*&outcome=&actor_id=&resource_type=&resource_id=&since=&until=` | Full-text, filterable audit log (`audit:read`), paged with `before_id` |
| `GET /healthz`, `GET /readyz`, `GET /metrics` | Liveness, readiness (DB + Redis), Prometheus |

## Webhooks

Every published `trigger.webhook` gets a URL `…/api/v1/hooks/<token>`. With the default `signature`
authentication, sign each request:

```python
import hashlib, hmac, json, time, httpx

secret = "whsec_…"                      # GET …/trigger?reveal_secret=true
body = json.dumps({"email": "jane@globex.com", "subject": "Charged twice"}).encode()
ts = str(int(time.time()))
sig = "v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
httpx.post(url, content=body, headers={
    "Content-Type": "application/json",
    "X-FlowForge-Timestamp": ts,          # must be within 5 minutes
    "X-FlowForge-Signature": sig,
    "Idempotency-Key": "evt_123",         # optional dedupe
})
```

Responses:

* `202 {"execution_id", "status", "duplicate": false}`: accepted (durably queued);
* `200 {…, "duplicate": true}`: a repeat of an earlier `Idempotency-Key`;
* `401`: bad or expired signature; `404`: unknown token or disabled trigger; `429`: rate limited.

With `response_mode: "wait_for_response"`, the request waits (up to `FF_WEBHOOK_SYNC_TIMEOUT_SECONDS`) for a
`comm.webhook_response` node and returns its status, headers and body, for example to build a synchronous
API on top of a workflow. If the workflow takes longer, the call returns `202` with the execution id.

The workflow sees the request as `trigger.body`, `trigger.headers` (signature headers removed),
`trigger.query`, `trigger.method` and `trigger.received_at`.
