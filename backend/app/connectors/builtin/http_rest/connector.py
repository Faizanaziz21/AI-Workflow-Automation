from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx

from app.connectors.builtin.http_rest.client import join_url, perform_request
from app.connectors.builtin.http_rest.schemas import RequestInput, RequestOutput, RestConfig, RestCredentials
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


class RestApiConnector(Connector):
    key = "http_rest"
    name = "REST API"
    description = "Generic REST/JSON API connector with API-key, bearer, basic or OAuth2 client-credentials auth."
    category = "integration"
    icon = "globe"
    auth = AuthSpec(
        AuthType.CUSTOM,
        credentials_model=RestCredentials,
        config_model=RestConfig,
        description="Choose the auth mode in configuration; supply matching credentials.",
    )

    async def _auth_parts(self, ctx: ConnectorContext) -> tuple[dict[str, str], dict[str, Any], httpx.Auth | None]:
        cfg: RestConfig = ctx.config
        creds: RestCredentials = ctx.credentials
        headers: dict[str, str] = dict(cfg.default_headers)
        query: dict[str, Any] = {}
        auth: httpx.Auth | None = None
        if cfg.auth_mode == "api_key":
            if not creds.api_key:
                raise ConnectorError("API key not configured")
            if cfg.api_key_in == "header":
                headers[cfg.api_key_name] = creds.api_key
            else:
                query[cfg.api_key_name] = creds.api_key
        elif cfg.auth_mode == "bearer":
            if not creds.token:
                raise ConnectorError("Bearer token not configured")
            headers["Authorization"] = f"Bearer {creds.token}"
        elif cfg.auth_mode == "basic":
            auth = httpx.BasicAuth(creds.username or "", creds.password or "")
        elif cfg.auth_mode == "oauth2_client_credentials":
            token = await self._client_credentials_token(ctx)
            headers["Authorization"] = f"Bearer {token}"
        return headers, query, auth

    async def _client_credentials_token(self, ctx: ConnectorContext, force: bool = False) -> str:
        cfg: RestConfig = ctx.config
        creds: RestCredentials = ctx.credentials
        if not force and creds.access_token and (creds.access_token_expires_at or 0) > time.time() + 60:
            return creds.access_token
        if not cfg.token_url or not creds.client_id or not creds.client_secret:
            raise ConnectorError("OAuth2 client-credentials requires token_url, client_id and client_secret")
        data = {"grant_type": "client_credentials"}
        if cfg.scope:
            data["scope"] = cfg.scope
        resp = await ctx.http.post(
            str(cfg.token_url), data=data, auth=httpx.BasicAuth(creds.client_id, creds.client_secret), timeout=20
        )
        raise_for_status(resp, "OAuth2 token endpoint")
        payload = resp.json()
        creds.access_token = payload["access_token"]
        creds.access_token_expires_at = time.time() + float(payload.get("expires_in", 3600))
        if ctx.save_credentials:
            await ctx.save_credentials(creds.model_dump())
        return creds.access_token

    def needs_refresh(self, ctx: ConnectorContext, now: datetime) -> bool:
        cfg: RestConfig = ctx.config
        creds: RestCredentials = ctx.credentials
        return cfg.auth_mode == "oauth2_client_credentials" and (creds.access_token_expires_at or 0) < now.timestamp()

    async def refresh_credentials(self, ctx: ConnectorContext) -> dict[str, Any] | None:
        if ctx.config.auth_mode != "oauth2_client_credentials":
            return None
        await self._client_credentials_token(ctx, force=True)
        result: dict[str, Any] = ctx.credentials.model_dump()
        return result

    @action(
        "request",
        "HTTP request",
        input=RequestInput,
        output=RequestOutput,
        description="Call any endpoint of the configured API.",
    )
    async def request(self, ctx: ConnectorContext, data: RequestInput) -> RequestOutput:
        headers, query, auth = await self._auth_parts(ctx)
        url = join_url(str(ctx.config.base_url), data.path)
        return await perform_request(
            ctx.http,
            url,
            data,
            extra_headers=headers,
            extra_query=query,
            auth=auth,
            idempotency_key=ctx.idempotency_key,
            service=self.name,
        )

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            out = await self.request(
                ctx,
                RequestInput(method="GET", path=ctx.config.health_path, fail_on_http_error=False, timeout_seconds=10),
            )
        except ConnectorError as exc:
            return TestResult(False, exc.message)
        if out.status in (401, 403):
            return TestResult(False, f"Authentication rejected (HTTP {out.status})")
        return TestResult(
            out.status < 500, f"Reached API (HTTP {out.status}) in {out.elapsed_ms} ms", {"status": out.status}
        )
