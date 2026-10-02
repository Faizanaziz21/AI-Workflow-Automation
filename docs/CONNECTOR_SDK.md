# Connector SDK

A connector packages everything FlowForge needs to talk to an external system: its authentication scheme,
connection settings, **actions** (things a workflow can do) and **triggers** (things that can start a workflow).
Connectors are plain Python classes with Pydantic models. The platform derives everything else from them:

* a node type per action (`hubspot.upsert_contact`), with a config form generated from the input model;
* the connection form (config and credential fields) in the console;
* JSON schemas for `GET /connectors` and `GET /node-types`;
* retry behaviour (from `ConnectorError.retryable`) and secret masking.

Code: `backend/app/connectors/sdk.py`, `registry.py`, `capabilities.py`, and the built-in packages under
`backend/app/connectors/builtin/`.

## Built-in connectors

| Key | System | Auth | Actions (capability) | Triggers |
|---|---|---|---|---|
| `http_rest` | Any REST/JSON API | none / API key / bearer / basic (custom) | `request` | |
| `slack` | Slack | bot token | `post_message`, `direct_message`, `lookup_user` | |
| `teams` | Microsoft Teams | incoming-webhook URL | `post_message` | |
| `email` | SMTP + IMAP | username/password | `send_email` (`email.send`) | `new_email` |
| `sendgrid` | SendGrid | API key | `send_email` (`email.send`) | |
| `postgres` | PostgreSQL | username/password | `query` (parameterised; read-only option) | `new_rows` (cursor CDC) |
| `mysql` | MySQL | username/password | `query` | `new_rows` |
| `hubspot` | HubSpot CRM | private-app token | `find_contact`, `upsert_contact`, `create_task`, `add_note` (`crm.*`), `create_deal` | `new_contacts` |
| `pipedrive` | Pipedrive CRM | API token | `find_contact`, `upsert_contact`, `create_task`, `add_note` (`crm.*`) | |
| `jira` | Jira Cloud | email + API token | `create_issue` (`ticket.create`), `add_comment` (`ticket.comment`), `get_issue`, `search_issues`, `transition_issue` | `issues_updated` |
| `google_drive` | Google Drive | OAuth 2.0 + PKCE | `list_files`, `upload_file`, `download_file`, `create_folder` | `new_file` |
| `twilio` | Twilio | account SID + auth token | `send_sms` (`sms.send`) | |
| `vonage` | Vonage | API key + secret | `send_sms` (`sms.send`) | |
| `openai`, `anthropic`, `gemini`, `openai_compatible` | LLM providers | API key | used by the AI gateway (see [AI_LAYER.md](AI_LAYER.md)) | |

## Writing a connector

```python
from pydantic import BaseModel, Field, HttpUrl

from app.connectors.sdk import (
    AuthSpec, AuthType, Connector, ConnectorContext, ConnectorError, PollResult, TestResult, action, trigger,
)


class AcmeConfig(BaseModel):                      # non-secret settings, stored in plain JSON
    api_base_url: HttpUrl = HttpUrl("https://api.acme.example")


class AcmeCredentials(BaseModel):                 # secrets: envelope-encrypted, never returned by the API
    api_key: str = Field(description="Settings → API keys")


class CreateOrderInput(BaseModel):
    customer_email: str
    amount: float = Field(gt=0)


class Order(BaseModel):
    id: str
    status: str


class NewOrdersConfig(BaseModel):
    status: str = "paid"


class AcmeConnector(Connector):
    key = "acme"
    name = "Acme Commerce"
    description = "Orders and customers in Acme."
    category = "business"
    auth = AuthSpec(AuthType.API_KEY, credentials_model=AcmeCredentials, config_model=AcmeConfig)

    async def _call(self, ctx: ConnectorContext, method: str, path: str, **kw) -> dict:
        r = await ctx.http.request(                    # egress-policed shared client
            method, f"{ctx.config.api_base_url}{path}",
            headers={"Authorization": f"Bearer {ctx.credentials.api_key}"}, timeout=ctx.timeout, **kw,
        )
        if r.status_code == 404:
            raise ConnectorError("Order not found", retryable=False, status_code=404)
        raise_for_status(r, self.name)                 # 408/409/425/429/5xx → retryable
        return r.json()

    @action("create_order", "Create order", input=CreateOrderInput, output=Order)
    async def create_order(self, ctx: ConnectorContext, data: CreateOrderInput) -> Order:
        headers = {"Idempotency-Key": ctx.idempotency_key} if ctx.idempotency_key else {}
        return Order.model_validate(await self._call(ctx, "POST", "/orders", json=data.model_dump(), headers=headers))

    @trigger("new_orders", "New order", config=NewOrdersConfig, output=Order, interval=60)
    async def new_orders(self, ctx: ConnectorContext, cfg: NewOrdersConfig, state: dict) -> PollResult:
        cursor = state.get("cursor", 0)
        page = await self._call(ctx, "GET", "/orders", params={"status": cfg.status, "after": cursor})
        items = [Order.model_validate(o).model_dump() for o in page["data"]]
        return PollResult(items=items, state={"cursor": page.get("next_cursor", cursor)})

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            await self._call(ctx, "GET", "/me")
        except ConnectorError as exc:
            return TestResult(False, exc.message)
        return TestResult(True, "API key accepted")
```

(`raise_for_status` is imported from `app.connectors.sdk` as well.)

### The pieces

| Element | Purpose |
|---|---|
| `key`, `name`, `description`, `category`, `icon`, `version`, `docs_url` | Catalog metadata. `category` places generated nodes in the palette (`communication`, `business`, `data`, `ai`, …) |
| `auth = AuthSpec(type, credentials_model, config_model, oauth2, description)` | `type` is `none`, `api_key`, `bearer`, `basic`, `oauth2` or `custom`. The two models define the connection form. Only `credentials_model` fields are encrypted |
| `@action(key, name, input=, output=, idempotent=False, capability=None)` | Registers an action. The input model becomes the node's config schema. Output must validate against `output` |
| `@trigger(key, name, config=, output=, interval=60)` | Registers a polling trigger. Receives the persisted `state`, returns new items plus the new state (cursor) |
| `test_connection(ctx)` | Backs `POST /connections/{id}/test` and the connection health badge |
| `needs_refresh(ctx, now)`, `refresh_credentials(ctx)` | OAuth 2.0 token refresh. Called automatically before an action, and again after an `AuthExpiredError` |
| `exchange_code(ctx, code, redirect_uri, code_verifier)` | OAuth 2.0 authorization-code exchange, used by the `oauth/start` → `oauth/callback` flow (PKCE verifier and state are held in Redis for 10 minutes) |

### `ConnectorContext`

| Field | Description |
|---|---|
| `config` | Validated `config_model` instance |
| `credentials` | Validated `credentials_model` instance, decrypted in memory for this call only |
| `http` | Shared `httpx.AsyncClient` behind the **egress policy** (see below) |
| `idempotency_key` | Stable per node and loop scope across retries. Send it on writes where the API supports it |
| `timeout` | Remaining time budget for the call |
| `log(level, message, data)` | Appears in the execution timeline. Data is masked |
| `files` | `FileStore` for exchanging binary files with the platform (attachments, documents) |
| `save_credentials(dict)` | Persist refreshed credentials (re-encrypted, version bumped) |
| `org_id`, `connection_id`, `extras` | Context for multi-tenant connectors |

### Errors and retries

Raise `ConnectorError(message, retryable=…, status_code=…, details=…)`. The engine retries only retryable errors
(unless a node sets `retry_on: "all"`). `raise_for_status(response, service)` maps HTTP statuses consistently:
`401` → `AuthExpiredError` (triggers one refresh for OAuth connectors), `408/409/425/429/5xx` → retryable,
other `4xx` → not retryable. httpx timeouts and transport errors raised inside an action are converted to
retryable errors by `run_action`, so connector code does not need to catch them.

Never put credential values in error messages. They are masked anyway (values of the connection's secrets
are scrubbed from outputs, errors, events and logs), but masking is a safety net, not the design.

### Egress (SSRF) policy

`ctx.http` refuses requests to loopback, RFC 1918, link-local (including cloud metadata `169.254.169.254`),
CGNAT, multicast and reserved addresses, and to non-HTTP schemes. The check runs in the transport for every
request, redirects included, against the addresses the host name resolves to. Hosts in `FF_EGRESS_ALLOWLIST`
(glob patterns) are exempt, and `FF_ALLOW_PRIVATE_NETWORK_EGRESS=true` disables the check for development.
Connectors should use `ctx.http` rather than creating their own clients. The SQL and IMAP/SMTP connectors open
their own sockets to the configured host: that host is set on the connection by a connection manager, not by
workflow authors.

## Capabilities: provider-neutral nodes

Some operations exist in many products: send an email, send an SMS, upsert a CRM contact, open a ticket.
`capabilities.py` defines a shared input/output contract for each, and actions can declare
`capability="crm.upsert_contact"`. The platform then generates one provider-neutral node (`crm.upsert_contact`)
whose connection picker lists every connection that supports the capability:

| Capability | Generated node | Implemented by |
|---|---|---|
| `email.send` | `comm.email` | `email` (SMTP), `sendgrid` |
| `sms.send` | `comm.sms` | `twilio`, `vonage` |
| `crm.find_contact`, `crm.upsert_contact`, `crm.create_task`, `crm.add_note` | same name | `hubspot`, `pipedrive` |
| `ticket.create`, `ticket.comment` | same name | `jira` |

Switching a workflow from HubSpot to Pipedrive, or from Twilio to Vonage, is a change of connection. The
workflow definition stays the same. The templates use capability nodes for this reason.

## Triggers

A connector `@trigger` is reachable in two ways:

* **Dedicated trigger nodes** with tailored forms: `trigger.email` (IMAP `new_email`), `trigger.db_change`
  (PostgreSQL/MySQL `new_rows`), `trigger.file_uploaded` with the Google Drive source (`new_file`).
* **`trigger.app_event` ("App trigger")** for any connector trigger: choose a connection, the trigger key
  (`event`, for example `new_contacts`) and its `settings`.

The scheduler enqueues a `trigger.poll` job when a trigger is due. The job calls the connector's trigger with
the persisted cursor and starts one execution per returned item, each with an idempotency key derived from
the item, so a re-polled item never starts a second execution. Errors (bad credentials, unknown trigger key,
invalid settings) are stored as the trigger's `last_error`, shown in the editor, and back off the poll interval.

Push-style integrations (Stripe, GitHub, Shopify webhooks) need no connector code. Use `trigger.webhook` with
signature verification, optionally followed by a connector action.

## Registration and distribution

Built-in connectors are registered in `registry.py`. Third-party packages register through the
`flowforge.connectors` entry-point group without forking the platform:

```toml
# pyproject.toml of your package
[project.entry-points."flowforge.connectors"]
acme = "acme_flowforge.connector:AcmeConnector"
```

Install the package into the API and worker images and restart. The connector appears in `GET /connectors`, its
actions appear in the node palette as `acme.create_order`, and its trigger can be used through the App trigger.
Node types can be contributed the same way through the `flowforge.nodes` group.

## Connections at runtime

1. A workflow node stores only `connection_id`.
2. Saving or publishing a workflow checks that every referenced connection exists in the workflow's
   workspace and that the author may use it (the connection's access policy restricts it to roles or teams).
3. When the node runs, `EngineRuntime.connection(id)` loads the connection **in the execution's organization
   and workspace** (foreign ids are not found), decrypts the credentials (AES-256-GCM data key unwrapped by the active KEK), validates them against the
   connector's models, refreshes OAuth tokens if needed, and registers every secret value with the output masker.
4. The action runs with a fresh `ConnectorContext`. Nothing of the credentials is persisted in node runs.

Rotating credentials (`POST /connections/{id}/rotate`) writes a new secret version. Running and future
executions pick it up on their next node, and no workflow needs editing.

## Testing a connector

`backend/tests/test_connectors.py` shows the patterns:

* Mock HTTP with `respx` and assert on the outgoing request: auth header, idempotency key, body mapping.
* Assert error mapping: `503` must be retryable, `400` must not, `401` must trigger one refresh.
* For polling triggers, assert that the cursor advances and that a second poll returns nothing new.
* For SQL connectors, run against the test database (`sqlutil` enforces identifier validation and bound
  parameters).

End-to-end, the `mock-services` sandbox implements protocol-compatible subsets of HubSpot, Jira, Slack,
SendGrid and an OpenAI-compatible LLM, so whole workflows can run against the real connectors offline.
