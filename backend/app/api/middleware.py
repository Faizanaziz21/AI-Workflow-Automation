"""HTTP middleware: request IDs, security headers, rate limiting, body-size limits, metrics.

Written as plain ASGI middleware rather than ``BaseHTTPMiddleware``: the latter runs every request through an extra
task, memory streams and a task group per layer, which measurably added CPU per request on the webhook hot path.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.logging import request_id_var
from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS
from app.core.principal import RequestMeta, request_meta_var
from app.core.redis import RateLimiter

logger = logging.getLogger("flowforge.http")


def client_ip(request: Request) -> str:
    return _client_ip(request.headers, request.scope)


def _client_ip(headers: Headers, scope: Scope) -> str:
    settings = get_settings()
    forwarded = headers.get("x-forwarded-for")
    if forwarded and settings.trusted_proxies > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            idx = max(0, len(parts) - settings.trusted_proxies)
            return parts[idx]
    client = scope.get("client")
    return client[0] if client else "unknown"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        rid = headers.get("x-request-id") or uuid.uuid4().hex
        if len(rid) > 64 or not rid.replace("-", "").isalnum():
            rid = uuid.uuid4().hex
        token = request_id_var.set(rid)
        meta_token = request_meta_var.set(
            RequestMeta(ip_address=_client_ip(headers, scope), user_agent=headers.get("user-agent"), request_id=rid)
        )
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            elapsed = time.perf_counter() - start
            request_id_var.reset(token)
            request_meta_var.reset(meta_token)
            route_label = getattr(scope.get("route"), "path", "unmatched")
            HTTP_REQUESTS.labels(scope["method"], route_label, str(status)).inc()
            HTTP_LATENCY.labels(scope["method"], route_label).observe(elapsed)


_DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https:"
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        production = get_settings().is_production

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("X-Frame-Options", "DENY")
                h.setdefault("Referrer-Policy", "no-referrer")
                h.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
                h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                if path.startswith(("/docs", "/redoc")):
                    h.setdefault("Content-Security-Policy", _DOCS_CSP)
                else:
                    h.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                if production:
                    h.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
                if path.startswith("/api/"):
                    h.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RateLimitMiddleware:
    """Per-identity rate limiting. Identity = API key prefix / bearer token hash / client IP."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter | None = None) -> None:
        self.app = app
        self.limiter = limiter or RateLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if not path.startswith("/api/") or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        headers = Headers(scope=scope)
        if path.startswith("/api/v1/auth/login") or path.startswith("/api/v1/auth/register"):
            bucket, limit = f"auth:{_client_ip(headers, scope)}", settings.rate_limit_auth
        elif path.startswith("/api/v1/hooks/"):
            bucket, limit = f"hook:{path.rsplit('/', 1)[-1][:16]}", settings.rate_limit_webhook
        else:
            ident = headers.get("x-api-key") or headers.get("authorization") or _client_ip(headers, scope)
            bucket = "id:" + hashlib.sha256(ident.encode()).hexdigest()[:24]
            limit = settings.rate_limit_default
        try:
            result = await self.limiter.hit(bucket, limit)
        except Exception:  # Redis outage must not take the API down; fail open but log.
            logger.warning("rate limiter unavailable; allowing request", exc_info=True)
            await self.app(scope, receive, send)
            return
        if not result.allowed:
            retry_after = max(1, result.retry_after_ms // 1000)
            response = JSONResponse(
                {"error": {"code": "rate_limited", "message": "Too many requests"}},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            length = Headers(scope=scope).get("content-length")
            limit = get_settings().max_upload_bytes + 1024 * 64
            if length and length.isdigit() and int(length) > limit:
                response = JSONResponse(
                    {"error": {"code": "payload_too_large", "message": "Request body too large"}}, 413
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
