from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx

from app.connectors.sdk import AuthExpiredError, ConnectorError, raise_for_status

FILE_FIELDS = "id,name,mimeType,size,createdTime,modifiedTime,webViewLink,parents"


async def refresh_access_token(http: httpx.AsyncClient, token_url: str, client_id: str, creds: Any) -> dict[str, Any]:
    if not creds.refresh_token:
        raise AuthExpiredError("Google Drive connection has no refresh token; re-authorize the connection")
    resp = await http.post(
        token_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": creds.refresh_token,
            "client_id": client_id,
            "client_secret": creds.client_secret,
        },
    )
    if resp.status_code in (400, 401):
        raise AuthExpiredError("Google rejected the refresh token; re-authorize the connection")
    raise_for_status(resp, "Google OAuth")
    payload = resp.json()
    return {
        "client_secret": creds.client_secret,
        "refresh_token": payload.get("refresh_token") or creds.refresh_token,
        "access_token": payload["access_token"],
        "expires_at": time.time() + float(payload.get("expires_in", 3600)),
    }


class DriveClient:
    def __init__(self, http: httpx.AsyncClient, base: str, access_token: str) -> None:
        self.http = http
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {access_token}"}

    async def request(self, method: str, path: str, *, upload: bool = False, raw: bool = False, **kwargs: Any) -> Any:
        prefix = "/upload/drive/v3" if upload else "/drive/v3"
        resp = await self.http.request(
            method, f"{self.base}{prefix}{path}", headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs
        )
        raise_for_status(resp, "Google Drive")
        if raw:
            return resp
        return resp.json() if resp.content else None

    async def multipart_upload(self, metadata: dict[str, Any], content: bytes, mime_type: str) -> dict[str, Any]:
        boundary = f"ff-{uuid.uuid4().hex}"
        body = (
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n"
                f"--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n"
            ).encode()
            + content
            + f"\r\n--{boundary}--".encode()
        )
        result: dict[str, Any] = await self.request(
            "POST",
            "/files",
            upload=True,
            params={"uploadType": "multipart", "fields": FILE_FIELDS},
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        if not isinstance(result, dict):
            raise ConnectorError("Unexpected upload response")
        return result
