from __future__ import annotations

from typing import Any

import httpx

from app.connectors.sdk import raise_for_status

CONTACT_PROPERTIES = ["email", "firstname", "lastname", "company", "phone", "jobtitle", "lifecyclestage", "createdate"]
# HubSpot-defined association type ids
TASK_TO_CONTACT = 204
NOTE_TO_CONTACT = 202
DEAL_TO_CONTACT = 3


class HubSpotClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, token: str, idempotency_key: str | None = None) -> None:
        self.http = http
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.idempotency_key = idempotency_key

    async def request(self, method: str, path: str, json: Any = None, params: dict[str, Any] | None = None) -> Any:
        headers = dict(self.headers)
        if self.idempotency_key and method in ("POST", "PATCH"):
            headers["Idempotency-Key"] = self.idempotency_key
        resp = await self.http.request(method, f"{self.base}{path}", json=json, params=params, headers=headers)
        raise_for_status(resp, "HubSpot")
        return resp.json() if resp.content else None

    async def search_contacts(
        self, filters: list[dict[str, Any]], *, limit: int = 1, sorts: list[dict] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "filterGroups": [{"filters": filters}],
            "properties": CONTACT_PROPERTIES,
            "limit": limit,
        }
        if sorts:
            body["sorts"] = sorts
        result: dict[str, Any] = await self.request("POST", "/crm/v3/objects/contacts/search", json=body)
        return result
