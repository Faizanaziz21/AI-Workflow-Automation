from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.connectors.builtin.hubspot.client import (
    DEAL_TO_CONTACT,
    NOTE_TO_CONTACT,
    TASK_TO_CONTACT,
    HubSpotClient,
)
from app.connectors.builtin.hubspot.schemas import (
    CreateDealInput,
    CreateDealOutput,
    HubSpotConfig,
    HubSpotCredentials,
    NewContactsConfig,
)
from app.connectors.capabilities import (
    AddNoteInput,
    AddNoteOutput,
    CreateTaskInput,
    CreateTaskOutput,
    CrmContact,
    FindContactInput,
    FindContactOutput,
    UpsertContactInput,
    UpsertContactOutput,
)
from app.connectors.sdk import (
    AuthSpec,
    AuthType,
    Connector,
    ConnectorContext,
    ConnectorError,
    PollResult,
    TestResult,
    action,
    trigger,
)


def _assoc(object_id: str, type_id: int) -> list[dict[str, Any]]:
    return [
        {"to": {"id": object_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}]}
    ]


class HubSpotConnector(Connector):
    key = "hubspot"
    name = "HubSpot CRM"
    description = "Contacts, deals, tasks and notes in HubSpot CRM (v3 API)."
    category = "crm"
    icon = "hubspot"
    docs_url = "https://developers.hubspot.com/docs/api/crm/contacts"
    auth = AuthSpec(
        AuthType.BEARER,
        credentials_model=HubSpotCredentials,
        config_model=HubSpotConfig,
        description="Create a private app with crm.objects.contacts/deals read+write scopes.",
    )

    def _client(self, ctx: ConnectorContext) -> HubSpotClient:
        return HubSpotClient(ctx.http, str(ctx.config.api_base_url), ctx.credentials.access_token, ctx.idempotency_key)

    def _contact(self, ctx: ConnectorContext, obj: dict[str, Any]) -> CrmContact:
        p = obj.get("properties", {})
        portal = ctx.config.portal_id
        return CrmContact(
            id=str(obj["id"]),
            email=p.get("email"),
            first_name=p.get("firstname"),
            last_name=p.get("lastname"),
            company=p.get("company"),
            phone=p.get("phone"),
            job_title=p.get("jobtitle"),
            lifecycle_stage=p.get("lifecyclestage"),
            properties=p,
            url=f"https://app.hubspot.com/contacts/{portal}/contact/{obj['id']}" if portal else None,
        )

    @action(
        "find_contact",
        "Find contact by email",
        input=FindContactInput,
        output=FindContactOutput,
        capability="crm.find_contact",
        idempotent=True,
    )
    async def find_contact(self, ctx: ConnectorContext, data: FindContactInput) -> FindContactOutput:
        result = await self._client(ctx).search_contacts(
            [{"propertyName": "email", "operator": "EQ", "value": str(data.email)}]
        )
        results = result.get("results", [])
        if not results:
            return FindContactOutput(found=False)
        return FindContactOutput(found=True, contact=self._contact(ctx, results[0]))

    @action(
        "upsert_contact",
        "Create or update contact",
        input=UpsertContactInput,
        output=UpsertContactOutput,
        capability="crm.upsert_contact",
        idempotent=True,
        description="Find the contact by email and update it, or create it when missing (deduplicating).",
    )
    async def upsert_contact(self, ctx: ConnectorContext, data: UpsertContactInput) -> UpsertContactOutput:
        props: dict[str, Any] = {
            "email": str(data.email),
            "firstname": data.first_name,
            "lastname": data.last_name,
            "company": data.company,
            "phone": data.phone,
            "jobtitle": data.job_title,
            "lifecyclestage": data.lifecycle_stage,
            **data.properties,
        }
        props = {k: v for k, v in props.items() if v is not None}
        client = self._client(ctx)
        existing = await self.find_contact(ctx, FindContactInput(email=data.email))
        if existing.found and existing.contact and existing.contact.id:
            obj = await client.request(
                "PATCH", f"/crm/v3/objects/contacts/{existing.contact.id}", json={"properties": props}
            )
            return UpsertContactOutput(contact=self._contact(ctx, obj), created=False)
        try:
            obj = await client.request("POST", "/crm/v3/objects/contacts", json={"properties": props})
        except ConnectorError as exc:
            if exc.status_code == 409:  # created concurrently (e.g. retried request) — fetch and update
                again = await self.find_contact(ctx, FindContactInput(email=data.email))
                if again.contact and again.contact.id:
                    obj = await client.request(
                        "PATCH", f"/crm/v3/objects/contacts/{again.contact.id}", json={"properties": props}
                    )
                    return UpsertContactOutput(contact=self._contact(ctx, obj), created=False)
            raise
        return UpsertContactOutput(contact=self._contact(ctx, obj), created=True)

    @action("create_task", "Create task", input=CreateTaskInput, output=CreateTaskOutput, capability="crm.create_task")
    async def create_task(self, ctx: ConnectorContext, data: CreateTaskInput) -> CreateTaskOutput:
        due = data.due_at or datetime.now(UTC).isoformat()
        body: dict[str, Any] = {
            "properties": {
                "hs_task_subject": data.subject,
                "hs_task_body": data.body,
                "hs_timestamp": due,
                "hs_task_priority": data.priority,
                "hs_task_status": "NOT_STARTED",
                **({"hubspot_owner_id": data.owner_id} if data.owner_id else {}),
            }
        }
        if data.contact_id:
            body["associations"] = _assoc(data.contact_id, TASK_TO_CONTACT)
        obj = await self._client(ctx).request("POST", "/crm/v3/objects/tasks", json=body)
        return CreateTaskOutput(task_id=str(obj["id"]))

    @action("add_note", "Add note to contact", input=AddNoteInput, output=AddNoteOutput, capability="crm.add_note")
    async def add_note(self, ctx: ConnectorContext, data: AddNoteInput) -> AddNoteOutput:
        body = {
            "properties": {"hs_note_body": data.body, "hs_timestamp": datetime.now(UTC).isoformat()},
            "associations": _assoc(data.contact_id, NOTE_TO_CONTACT),
        }
        obj = await self._client(ctx).request("POST", "/crm/v3/objects/notes", json=body)
        return AddNoteOutput(note_id=str(obj["id"]))

    @action("create_deal", "Create deal", input=CreateDealInput, output=CreateDealOutput)
    async def create_deal(self, ctx: ConnectorContext, data: CreateDealInput) -> CreateDealOutput:
        props: dict[str, Any] = {
            "dealname": data.name,
            "dealstage": data.stage,
            "pipeline": data.pipeline,
            **data.properties,
        }
        if data.amount is not None:
            props["amount"] = str(data.amount)
        if data.close_date:
            props["closedate"] = data.close_date
        body: dict[str, Any] = {"properties": props}
        if data.contact_id:
            body["associations"] = _assoc(data.contact_id, DEAL_TO_CONTACT)
        obj = await self._client(ctx).request("POST", "/crm/v3/objects/deals", json=body)
        portal = ctx.config.portal_id
        return CreateDealOutput(
            deal_id=str(obj["id"]),
            url=f"https://app.hubspot.com/contacts/{portal}/deal/{obj['id']}" if portal else None,
        )

    @trigger("new_contacts", "New contact created", config=NewContactsConfig, output=CrmContact, interval=60)
    async def new_contacts(self, ctx: ConnectorContext, cfg: NewContactsConfig, state: dict) -> PollResult:
        since = state.get("since") or datetime.now(UTC).isoformat()
        since_ms = int(datetime.fromisoformat(since).timestamp() * 1000)
        result = await self._client(ctx).search_contacts(
            [{"propertyName": "createdate", "operator": "GT", "value": str(since_ms)}],
            limit=cfg.batch_size,
            sorts=[{"propertyName": "createdate", "direction": "ASCENDING"}],
        )
        items = [self._contact(ctx, obj).model_dump() for obj in result.get("results", [])]
        new_since = since
        for obj in result.get("results", []):
            created = obj.get("properties", {}).get("createdate")
            if created:
                new_since = max(new_since, created.replace("Z", "+00:00"))
        return PollResult(items=items, state={"since": new_since})

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            await self._client(ctx).request("GET", "/crm/v3/objects/contacts", params={"limit": 1})
        except ConnectorError as exc:
            return TestResult(False, exc.message)
        return TestResult(True, "HubSpot API reachable and token accepted")
