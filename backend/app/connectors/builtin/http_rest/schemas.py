from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class RestConfig(BaseModel):
    base_url: HttpUrl = Field(description="Base URL of the API, e.g. https://erp.example.com/api")
    auth_mode: Literal["none", "api_key", "bearer", "basic", "oauth2_client_credentials"] = "none"
    api_key_name: str = Field(default="X-API-Key", description="Header or query parameter carrying the API key")
    api_key_in: Literal["header", "query"] = "header"
    token_url: HttpUrl | None = Field(default=None, description="OAuth2 token endpoint (client credentials)")
    scope: str | None = None
    default_headers: dict[str, str] = {}
    health_path: str = Field(default="/", description="Path requested by 'Test connection'")


class RestCredentials(BaseModel):
    api_key: str | None = None
    token: str | None = None
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    access_token: str | None = Field(default=None, description="Cached OAuth2 access token (managed)")
    access_token_expires_at: float | None = None


HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class RequestInput(BaseModel):
    method: HttpMethod = "GET"
    path: str = Field(default="", description="Path appended to the base URL, or an absolute URL")
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    json_body: Any | None = Field(default=None, description="JSON request body")
    text_body: str | None = Field(default=None, description="Raw request body (used when json_body is empty)")
    form_body: dict[str, str] | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    fail_on_http_error: bool = Field(default=True, description="Treat 4xx/5xx as node failure")
    response_format: Literal["auto", "json", "text"] = "auto"


class RequestOutput(BaseModel):
    status: int
    ok: bool
    headers: dict[str, str]
    body: Any
    url: str
    elapsed_ms: int
