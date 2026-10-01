from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from app.connectors.builtin.google_drive.client import FILE_FIELDS, DriveClient, refresh_access_token
from app.connectors.builtin.google_drive.schemas import (
    CreateFolderInput,
    DownloadFileInput,
    DownloadFileOutput,
    DriveConfig,
    DriveCredentials,
    DriveFile,
    ListFilesInput,
    ListFilesOutput,
    NewFilesConfig,
    UploadFileInput,
)
from app.connectors.sdk import (
    AuthSpec,
    AuthType,
    Connector,
    ConnectorContext,
    ConnectorError,
    OAuth2Spec,
    PollResult,
    TestResult,
    action,
    trigger,
)

FOLDER_MIME = "application/vnd.google-apps.folder"


def _q_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _file(raw: dict[str, Any]) -> DriveFile:
    return DriveFile(
        id=raw["id"],
        name=raw.get("name", ""),
        mime_type=raw.get("mimeType", ""),
        size=int(raw["size"]) if raw.get("size") else None,
        created_time=raw.get("createdTime"),
        modified_time=raw.get("modifiedTime"),
        web_view_link=raw.get("webViewLink"),
        parents=raw.get("parents", []),
    )


class GoogleDriveConnector(Connector):
    key = "google_drive"
    name = "Google Drive"
    description = "List, upload, download and watch files in Google Drive (OAuth2 with automatic token refresh)."
    category = "storage"
    icon = "google-drive"
    auth = AuthSpec(
        AuthType.OAUTH2,
        credentials_model=DriveCredentials,
        config_model=DriveConfig,
        oauth2=OAuth2Spec(
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive"],
        ),
        description="Authorize with Google; FlowForge stores the refresh token encrypted and refreshes access tokens.",
    )

    def needs_refresh(self, ctx: ConnectorContext, now: datetime) -> bool:
        creds: DriveCredentials = ctx.credentials
        return not creds.access_token or (creds.expires_at or 0) < now.timestamp() + 60

    async def refresh_credentials(self, ctx: ConnectorContext) -> dict[str, Any] | None:
        new = await refresh_access_token(ctx.http, str(ctx.config.token_url), ctx.config.client_id, ctx.credentials)
        ctx.credentials = DriveCredentials.model_validate(new)
        if ctx.save_credentials:
            await ctx.save_credentials(new)
        return new

    async def _client(self, ctx: ConnectorContext) -> DriveClient:
        if self.needs_refresh(ctx, datetime.now(UTC)):
            await self.refresh_credentials(ctx)
        return DriveClient(ctx.http, str(ctx.config.api_base_url), ctx.credentials.access_token or "")

    @action("list_files", "List files", input=ListFilesInput, output=ListFilesOutput, idempotent=True)
    async def list_files(self, ctx: ConnectorContext, data: ListFilesInput) -> ListFilesOutput:
        clauses = ["trashed = false"]
        folder = data.folder_id or ctx.config.default_folder_id
        if folder:
            clauses.append(f"'{_q_escape(folder)}' in parents")
        if data.name_contains:
            clauses.append(f"name contains '{_q_escape(data.name_contains)}'")
        if data.mime_type:
            clauses.append(f"mimeType = '{_q_escape(data.mime_type)}'")
        client = await self._client(ctx)
        raw = await client.request(
            "GET",
            "/files",
            params={
                "q": " and ".join(clauses),
                "pageSize": data.page_size,
                "fields": f"files({FILE_FIELDS})",
                "orderBy": "createdTime desc",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        return ListFilesOutput(files=[_file(f) for f in raw.get("files", [])])

    @action("upload_file", "Upload file", input=UploadFileInput, output=DriveFile)
    async def upload_file(self, ctx: ConnectorContext, data: UploadFileInput) -> DriveFile:
        if data.platform_file_id:
            if ctx.files is None:
                raise ConnectorError("File store unavailable")
            meta, content = await ctx.files.load(data.platform_file_id)
            mime = meta.get("content_type") or data.mime_type
        elif data.text_content is not None:
            content, mime = data.text_content.encode(), data.mime_type
        else:
            raise ConnectorError("Provide platform_file_id or text_content")
        folder = data.folder_id or ctx.config.default_folder_id
        metadata: dict[str, Any] = {"name": data.name}
        if folder:
            metadata["parents"] = [folder]
        if ctx.idempotency_key:
            metadata["appProperties"] = {"flowforge_key": ctx.idempotency_key[:120]}
            client = await self._client(ctx)
            existing = await client.request(
                "GET",
                "/files",
                params={
                    "q": f"appProperties has {{ key='flowforge_key' and value='{_q_escape(ctx.idempotency_key[:120])}' }} and trashed = false",
                    "fields": f"files({FILE_FIELDS})",
                },
            )
            if existing.get("files"):
                return _file(existing["files"][0])
        client = await self._client(ctx)
        return _file(await client.multipart_upload(metadata, content, mime))

    @action(
        "download_file",
        "Download file",
        input=DownloadFileInput,
        output=DownloadFileOutput,
        description="Download (or export) a Drive file into FlowForge file storage for downstream processing.",
    )
    async def download_file(self, ctx: ConnectorContext, data: DownloadFileInput) -> DownloadFileOutput:
        if ctx.files is None:
            raise ConnectorError("File store unavailable")
        client = await self._client(ctx)
        meta = _file(
            await client.request(
                "GET", f"/files/{data.file_id}", params={"fields": FILE_FIELDS, "supportsAllDrives": "true"}
            )
        )
        if meta.mime_type.startswith("application/vnd.google-apps"):
            export = data.export_mime_type or "application/pdf"
            resp = await client.request("GET", f"/files/{data.file_id}/export", params={"mimeType": export}, raw=True)
            mime = export
        else:
            resp = await client.request(
                "GET", f"/files/{data.file_id}", params={"alt": "media", "supportsAllDrives": "true"}, raw=True
            )
            mime = meta.mime_type
        stored = await ctx.files.save(meta.name, resp.content, mime, source="google_drive")
        preview = resp.content[:4000].decode(errors="replace") if mime.startswith("text/") else None
        return DownloadFileOutput(file=meta, platform_file=stored, text_preview=preview)

    @action("create_folder", "Create folder", input=CreateFolderInput, output=DriveFile)
    async def create_folder(self, ctx: ConnectorContext, data: CreateFolderInput) -> DriveFile:
        body: dict[str, Any] = {"name": data.name, "mimeType": FOLDER_MIME}
        parent = data.parent_id or ctx.config.default_folder_id
        if parent:
            body["parents"] = [parent]
        client = await self._client(ctx)
        return _file(await client.request("POST", "/files", json=body, params={"fields": FILE_FIELDS}))

    @trigger("new_file", "File uploaded to folder", config=NewFilesConfig, output=DriveFile, interval=60)
    async def new_file(self, ctx: ConnectorContext, cfg: NewFilesConfig, state: dict) -> PollResult:
        since = state.get("since") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        client = await self._client(ctx)
        raw = await client.request(
            "GET",
            "/files",
            params={
                "q": f"'{_q_escape(cfg.folder_id)}' in parents and trashed = false and createdTime > '{since}'",
                "fields": f"files({FILE_FIELDS})",
                "orderBy": "createdTime",
                "pageSize": 100,
            },
        )
        files = [_file(f) for f in raw.get("files", [])]
        new_since = files[-1].created_time[:19] if files and files[-1].created_time else since
        return PollResult(items=[f.model_dump() for f in files], state={"since": new_since})

    async def exchange_code(
        self, ctx: ConnectorContext, code: str, redirect_uri: str, code_verifier: str | None
    ) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": ctx.config.client_id,
            "client_secret": ctx.credentials.client_secret,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        resp = await ctx.http.post(str(ctx.config.token_url), data=data)
        if not resp.is_success:
            raise ConnectorError(f"Google token exchange failed (HTTP {resp.status_code})")
        payload = resp.json()
        return {
            "client_secret": ctx.credentials.client_secret,
            "refresh_token": payload.get("refresh_token"),
            "access_token": payload["access_token"],
            "expires_at": time.time() + float(payload.get("expires_in", 3600)),
        }

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            client = await self._client(ctx)
            about = await client.request("GET", "/about", params={"fields": "user(emailAddress,displayName)"})
        except ConnectorError as exc:
            return TestResult(False, exc.message)
        user = about.get("user", {})
        return TestResult(True, f"Connected as {user.get('emailAddress')}", {"user": user.get("displayName")})
