from __future__ import annotations

from typing import Any

import httpx

from app.connectors.sdk import AuthExpiredError, ConnectorError, raise_for_status


class SlackClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, token: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def call(self, method: str, payload: dict[str, Any] | None = None, *, get: bool = False) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.base_url}/{method}"
        if get:
            resp = await self.http.get(url, params=payload or {}, headers=headers)
        else:
            resp = await self.http.post(
                url, json=payload or {}, headers={**headers, "Content-Type": "application/json; charset=utf-8"}
            )
        if resp.status_code == 429:
            raise ConnectorError(
                "Slack rate limit exceeded",
                retryable=True,
                status_code=429,
                details={"retry_after": resp.headers.get("Retry-After")},
            )
        raise_for_status(resp, "Slack")
        data: dict[str, Any] = resp.json()
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            if error in ("invalid_auth", "token_revoked", "not_authed", "account_inactive", "token_expired"):
                raise AuthExpiredError(f"Slack auth error: {error}")
            retryable = error in ("ratelimited", "service_unavailable", "internal_error", "request_timeout")
            raise ConnectorError(f"Slack API error: {error}", retryable=retryable, details=data)
        return data
