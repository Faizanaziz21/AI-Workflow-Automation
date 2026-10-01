"""Shared HTTP request execution used by the REST connector and the HTTP Request node."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.connectors.builtin.http_rest.schemas import RequestInput, RequestOutput
from app.connectors.sdk import ConnectorError, raise_for_status

_RESPONSE_HEADER_ALLOWLIST = {
    "content-type",
    "content-length",
    "etag",
    "last-modified",
    "location",
    "retry-after",
    "x-request-id",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "link",
}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def join_url(base: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    if not path:
        return base
    return base.rstrip("/") + "/" + path.lstrip("/")


async def perform_request(
    http: httpx.AsyncClient,
    url: str,
    req: RequestInput,
    *,
    extra_headers: dict[str, str] | None = None,
    extra_query: dict[str, Any] | None = None,
    auth: httpx.Auth | None = None,
    idempotency_key: str | None = None,
    service: str = "HTTP",
) -> RequestOutput:
    headers = {**(extra_headers or {}), **req.headers}
    if idempotency_key and req.method in ("POST", "PUT", "PATCH", "DELETE"):
        headers.setdefault("Idempotency-Key", idempotency_key)
    params = {**(extra_query or {}), **{k: v for k, v in req.query.items() if v is not None}}
    kwargs: dict[str, Any] = {"headers": headers, "params": params, "timeout": req.timeout_seconds}
    if auth is not None:
        kwargs["auth"] = auth
    if req.json_body is not None:
        kwargs["json"] = req.json_body
    elif req.form_body is not None:
        kwargs["data"] = req.form_body
    elif req.text_body is not None:
        kwargs["content"] = req.text_body.encode()
    started = time.perf_counter()
    response = await http.request(req.method, url, **kwargs)
    elapsed = int((time.perf_counter() - started) * 1000)
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ConnectorError(f"Response body exceeds {MAX_RESPONSE_BYTES} bytes", retryable=False)
    if req.fail_on_http_error:
        raise_for_status(response, service)
    body: Any
    ctype = response.headers.get("content-type", "")
    if req.response_format == "text" or (req.response_format == "auto" and "json" not in ctype):
        body = response.text
        if req.response_format == "auto" and body and body.lstrip()[:1] in ("{", "["):
            try:
                body = response.json()
            except ValueError:
                pass
    else:
        try:
            body = response.json() if response.content else None
        except ValueError as exc:
            raise ConnectorError("Response is not valid JSON", retryable=False) from exc
    return RequestOutput(
        status=response.status_code,
        ok=response.is_success,
        headers={k: v for k, v in response.headers.items() if k.lower() in _RESPONSE_HEADER_ALLOWLIST},
        body=body,
        url=str(response.url),
        elapsed_ms=elapsed,
    )
