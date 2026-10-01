from __future__ import annotations

from typing import Any

from app.connectors.builtin.pipedrive.schemas import PipedriveConfig, PipedriveCredentials
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
    TestResult,
    action,
    raise_for_status,
)


class PipedriveConnector(Connector):
    key = "pipedrive"
    name = "Pipedrive CRM"
    description = "Persons, activities and notes in Pipedrive (v1 API)."
    category = "crm"
    icon = "pipedrive"
    auth = AuthSpec(AuthType.API_KEY, credentials_model=PipedriveCredentials, config_model=PipedriveConfig)

    def _base(self, ctx: ConnectorContext) -> str:
        cfg: PipedriveConfig = ctx.config
        return (
            str(cfg.api_base_url).rstrip("/")
            if cfg.api_base_url
            else f"https://{cfg.company_domain}.pipedrive.com/api/v1"
        )

    async def _req(self, ctx: ConnectorContext, method: str, path: str, **kwargs: Any) -> Any:
        resp = await ctx.http.request(
            method, f"{self._base(ctx)}{path}", headers={"x-api-token": ctx.credentials.api_token}, **kwargs
        )
        raise_for_status(resp, "Pipedrive")
        body = resp.json()
        if not body.get("success", True):
            raise ConnectorError(f"Pipedrive error: {body.get('error')}")
        return body.get("data")

    def _person(self, ctx: ConnectorContext, p: dict[str, Any]) -> CrmContact:
        emails = p.get("email") or p.get("emails") or []
        email = (
            next((e.get("value") for e in emails if isinstance(e, dict) and e.get("value")), None)
            if isinstance(emails, list)
            else emails
        )
        phones = p.get("phone") or []
        phone = (
            next((e.get("value") for e in phones if isinstance(e, dict) and e.get("value")), None)
            if isinstance(phones, list)
            else phones
        )
        org = p.get("org_name")
        if not org and isinstance(p.get("org_id"), dict):
            org = p["org_id"].get("name")
        return CrmContact(
            id=str(p["id"]),
            email=email,
            first_name=p.get("first_name"),
            last_name=p.get("last_name"),
            company=org,
            phone=phone,
            properties={"name": p.get("name")},
            url=f"https://{ctx.config.company_domain}.pipedrive.com/person/{p['id']}",
        )

    @action(
        "find_contact",
        "Find person by email",
        input=FindContactInput,
        output=FindContactOutput,
        capability="crm.find_contact",
        idempotent=True,
    )
    async def find_contact(self, ctx: ConnectorContext, data: FindContactInput) -> FindContactOutput:
        found = await self._req(
            ctx,
            "GET",
            "/persons/search",
            params={"term": str(data.email), "fields": "email", "exact_match": "true", "limit": 1},
        )
        items = (found or {}).get("items", [])
        if not items:
            return FindContactOutput(found=False)
        person = await self._req(ctx, "GET", f"/persons/{items[0]['item']['id']}")
        return FindContactOutput(found=True, contact=self._person(ctx, person))

    @action(
        "upsert_contact",
        "Create or update person",
        input=UpsertContactInput,
        output=UpsertContactOutput,
        capability="crm.upsert_contact",
        idempotent=True,
    )
    async def upsert_contact(self, ctx: ConnectorContext, data: UpsertContactInput) -> UpsertContactOutput:
        name = " ".join(x for x in (data.first_name, data.last_name) if x) or str(data.email)
        body: dict[str, Any] = {"name": name, "email": [{"value": str(data.email), "primary": True}], **data.properties}
        if data.first_name:
            body["first_name"] = data.first_name
        if data.last_name:
            body["last_name"] = data.last_name
        if data.phone:
            body["phone"] = [{"value": data.phone, "primary": True}]
        existing = await self.find_contact(ctx, FindContactInput(email=data.email))
        if existing.found and existing.contact:
            person = await self._req(ctx, "PUT", f"/persons/{existing.contact.id}", json=body)
            return UpsertContactOutput(contact=self._person(ctx, person), created=False)
        person = await self._req(ctx, "POST", "/persons", json=body)
        return UpsertContactOutput(contact=self._person(ctx, person), created=True)

    @action(
        "create_task", "Create activity", input=CreateTaskInput, output=CreateTaskOutput, capability="crm.create_task"
    )
    async def create_task(self, ctx: ConnectorContext, data: CreateTaskInput) -> CreateTaskOutput:
        body: dict[str, Any] = {"subject": data.subject, "note": data.body, "type": "task"}
        if data.due_at:
            body["due_date"] = data.due_at[:10]
        if data.contact_id:
            body["person_id"] = int(data.contact_id)
        if data.owner_id:
            body["user_id"] = int(data.owner_id)
        activity = await self._req(ctx, "POST", "/activities", json=body)
        return CreateTaskOutput(task_id=str(activity["id"]))

    @action("add_note", "Add note", input=AddNoteInput, output=AddNoteOutput, capability="crm.add_note")
    async def add_note(self, ctx: ConnectorContext, data: AddNoteInput) -> AddNoteOutput:
        note = await self._req(ctx, "POST", "/notes", json={"content": data.body, "person_id": int(data.contact_id)})
        return AddNoteOutput(note_id=str(note["id"]))

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            me = await self._req(ctx, "GET", "/users/me")
        except ConnectorError as exc:
            return TestResult(False, exc.message)
        return TestResult(True, f"Authenticated as {me.get('email')}")
