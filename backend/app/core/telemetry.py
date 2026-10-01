"""OpenTelemetry tracing setup.

Spans are created for every HTTP request, queue job and node execution. When
``FF_OTEL_EXPORTER_ENDPOINT`` is set, spans are exported via OTLP/HTTP (e.g. to an
OpenTelemetry Collector forwarding to Jaeger/Tempo); otherwise tracing is a no-op.
"""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.core.config import get_settings

logger = logging.getLogger(__name__)
_configured = False


def configure_tracer_provider(service_suffix: str = "") -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    _configured = True
    if not settings.otel_exporter_endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    resource = Resource.create(
        {"service.name": settings.otel_service_name + service_suffix, "deployment.environment": settings.env}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint.rstrip("/") + "/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    logger.info("OpenTelemetry tracing enabled", extra={"endpoint": settings.otel_exporter_endpoint})


def get_tracer(name: str = "flowforge") -> trace.Tracer:
    return trace.get_tracer(name)


class _TracingMiddleware:
    """Server span per HTTP request (plain ASGI; installed only when an exporter is configured)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.tracer = get_tracer("flowforge.http")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with self.tracer.start_as_current_span(
            f"{scope['method']} {scope['path']}",
            kind=SpanKind.SERVER,
            attributes={"http.method": scope["method"], "http.target": scope["path"]},
        ) as span:

            async def send_with_status(message: Any) -> None:
                if message["type"] == "http.response.start":
                    span.set_attribute("http.status_code", message["status"])
                    if message["status"] >= 500:
                        span.set_status(Status(StatusCode.ERROR))
                await send(message)

            await self.app(scope, receive, send_with_status)


def setup_tracing(app: Any) -> None:
    configure_tracer_provider("-api")
    if get_settings().otel_exporter_endpoint:
        app.add_middleware(_TracingMiddleware)
