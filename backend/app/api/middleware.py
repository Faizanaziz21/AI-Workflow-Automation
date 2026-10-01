"""HTTP middleware: request IDs, security headers, rate limiting, body-size limits, metrics."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core.config import get_settings
from app.core.logging import request_id_var
from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS
from app.core.principal import RequestMeta, request_meta_var
from app.core.redis import RateLimiter

logger = logging.getLogger("flowforge.http")


def client_ip(request: Request) -> str:
    settings = get_settings()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and settings.trusted_proxies > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            idx = max(0, len(parts) - settings.trusted_proxies)
            return parts[idx]
    return request.client.host if request.client else "unknown"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        if len(rid) > 64 or not rid.replace("-", "").isalnum():
            rid = uuid.uuid4().hex
        token = request_id_var.set(rid)
        meta_token = request_meta_var.set(
            RequestMeta(ip_address=client_ip(request), user_agent=request.headers.get("user-agent"), request_id=rid)
        )
        start = time.perf_counter()
        route_label = "unmatched"
        try:
            response = await call_next(request)
            route = request.scope.get("route")
            route_label = getattr(route, "path", "unmatched")
        finally:
            elapsed = time.perf_counter() - start
            request_id_var.reset(token)
            request_meta_var.reset(meta_token)
        HTTP_REQUESTS.labels(request.method, route_label, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route_label).observe(elapsed)
        response.headers["X-Request-ID"] = rid
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.url.path.startswith(("/docs", "/redoc")):
            h.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https:",
            )
        else:
            h.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if get_settings().is_production:
            h.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-identity rate limiting. Identity = API key prefix / bearer token hash / client IP."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter | None = None) -> None:
        super().__init__(app)
        self.limiter = limiter or RateLimiter()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if not path.startswith("/api/") or request.method == "OPTIONS":
            return await call_next(request)
        settings = get_settings()
        if path.startswith("/api/v1/auth/login") or path.startswith("/api/v1/auth/register"):
            bucket, limit = f"auth:{client_ip(request)}", settings.rate_limit_auth
        elif path.startswith("/api/v1/hooks/"):
            bucket, limit = f"hook:{path.rsplit('/', 1)[-1][:16]}", settings.rate_limit_webhook
        else:
            ident = request.headers.get("x-api-key") or request.headers.get("authorization") or client_ip(request)
            bucket = "id:" + hashlib.sha256(ident.encode()).hexdigest()[:24]
            limit = settings.rate_limit_default
        try:
            result = await self.limiter.hit(bucket, limit)
        except Exception:  # Redis outage must not take the API down; fail open but log.
            logger.warning("rate limiter unavailable; allowing request", exc_info=True)
            return await call_next(request)
        if not result.allowed:
            retry_after = max(1, result.retry_after_ms // 1000)
            return JSONResponse(
                {"error": {"code": "rate_limited", "message": "Too many requests"}},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        length = request.headers.get("content-length")
        limit = get_settings().max_upload_bytes + 1024 * 64
        if length and length.isdigit() and int(length) > limit:
            return JSONResponse({"error": {"code": "payload_too_large", "message": "Request body too large"}}, 413)
        return await call_next(request)
