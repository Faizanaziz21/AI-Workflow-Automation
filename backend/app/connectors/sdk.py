"""Connector SDK.

A connector is a self-contained integration package::

    connectors/builtin/<name>/
        __init__.py      # exports the Connector subclass
        connector.py     # metadata, auth spec, actions, triggers, test/refresh
        schemas.py       # pydantic models for config, credentials, action I/O
        client.py        # thin API client over the shared egress-controlled HTTP client

Actions are async methods decorated with :func:`action`; polling triggers with :func:`trigger`.
Input/output models double as JSON schemas for the visual editor, so a new connector shows up in the
node palette — with a generated configuration form — without touching the engine or the frontend.

Connectors never see the database or other tenants: they receive a :class:`ConnectorContext` holding the
validated config, decrypted credentials, an egress-controlled HTTP client, a logger and (optionally) the
file store.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Generic, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict

from app.core.egress import EgressDeniedError


class AuthType(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    BASIC = "basic"
    OAUTH2 = "oauth2"
    CUSTOM = "custom"


class EmptyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class OAuth2Spec:
    authorize_url: str
    token_url: str
    scopes: list[str]
    pkce: bool = True


@dataclass(frozen=True)
class AuthSpec:
    type: AuthType
    credentials_model: type[BaseModel] = EmptyModel
    config_model: type[BaseModel] = EmptyModel
    oauth2: OAuth2Spec | None = None
    description: str = ""


class ConnectorError(Exception):
    """Raised by connector actions. ``retryable`` drives the engine's retry policy."""

    def __init__(
        self, message: str, *, retryable: bool = False, status_code: int | None = None, details: Any = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.details = details


class AuthExpiredError(ConnectorError):
    def __init__(self, message: str = "Credentials expired or revoked") -> None:
        super().__init__(message, retryable=False, status_code=401)


class FileStore:
    """Narrow interface connectors use to exchange files with the platform."""

    async def save(self, filename: str, content: bytes, content_type: str, source: str) -> dict[str, Any]:
        raise NotImplementedError

    async def load(self, file_id: str) -> tuple[dict[str, Any], bytes]:
        raise NotImplementedError


@dataclass
class ConnectorContext:
    org_id: str
    connection_id: str | None
    config: Any
    credentials: Any
    http: httpx.AsyncClient
    log: Callable[[str, str, dict[str, Any] | None], None] = lambda level, msg, data=None: None
    idempotency_key: str | None = None
    files: FileStore | None = None
    save_credentials: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    timeout: float = 30.0
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestResult:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


InT = TypeVar("InT", bound=BaseModel)
OutT = TypeVar("OutT", bound=BaseModel)


@dataclass
class ActionSpec(Generic[InT, OutT]):
    key: str
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler_name: str
    idempotent: bool = False
    capability: str | None = None


@dataclass
class PollResult:
    items: list[dict[str, Any]]
    state: dict[str, Any]


@dataclass
class TriggerSpec:
    key: str
    name: str
    description: str
    config_model: type[BaseModel]
    output_model: type[BaseModel] | None
    handler_name: str
    default_interval_seconds: int = 60


def action(
    key: str,
    name: str,
    *,
    input: type[BaseModel],
    output: type[BaseModel],
    description: str = "",
    idempotent: bool = False,
    capability: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__ff_action__ = ActionSpec(  # type: ignore[attr-defined]
            key=key,
            name=name,
            description=description or (inspect.getdoc(fn) or ""),
            input_model=input,
            output_model=output,
            handler_name=fn.__name__,
            idempotent=idempotent,
            capability=capability,
        )
        return fn

    return deco


def trigger(
    key: str,
    name: str,
    *,
    config: type[BaseModel],
    output: type[BaseModel] | None = None,
    description: str = "",
    interval: int = 60,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__ff_trigger__ = TriggerSpec(  # type: ignore[attr-defined]
            key=key,
            name=name,
            description=description or (inspect.getdoc(fn) or ""),
            config_model=config,
            output_model=output,
            handler_name=fn.__name__,
            default_interval_seconds=interval,
        )
        return fn

    return deco


class Connector:
    """Base class for all connectors."""

    key: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str] = ""
    category: ClassVar[str] = "integration"
    icon: ClassVar[str] = "plug"
    version: ClassVar[str] = "1.0.0"
    docs_url: ClassVar[str | None] = None
    auth: ClassVar[AuthSpec] = AuthSpec(AuthType.NONE)

    actions: ClassVar[dict[str, ActionSpec[Any, Any]]]
    triggers: ClassVar[dict[str, TriggerSpec]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.actions = {}
        cls.triggers = {}
        for attr in dir(cls):
            member = getattr(cls, attr, None)
            spec = getattr(member, "__ff_action__", None)
            if spec is not None:
                cls.actions[spec.key] = spec
            tspec = getattr(member, "__ff_trigger__", None)
            if tspec is not None:
                cls.triggers[tspec.key] = tspec

    # -- lifecycle hooks -------------------------------------------------------------------------

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        return TestResult(True, "Connection has no remote check; configuration is valid")

    async def refresh_credentials(self, ctx: ConnectorContext) -> dict[str, Any] | None:
        """Return new credential payload if refresh happened (OAuth2), else ``None``."""
        return None

    def needs_refresh(self, ctx: ConnectorContext, now: datetime) -> bool:
        return False

    # -- execution ------------------------------------------------------------------------------

    async def run_action(self, key: str, ctx: ConnectorContext, data: dict[str, Any]) -> dict[str, Any]:
        spec = self.actions.get(key)
        if spec is None:
            raise ConnectorError(f"Unknown action '{key}' for connector '{self.key}'")
        payload = spec.input_model.model_validate(data)
        handler = getattr(self, spec.handler_name)
        try:
            result = await handler(ctx, payload)
        except AuthExpiredError:
            if self.auth.type != AuthType.OAUTH2:
                raise
            refreshed = await self.refresh_credentials(ctx)
            if not refreshed:
                raise
            result = await handler(ctx, payload)
        except EgressDeniedError as exc:
            raise ConnectorError(str(exc), retryable=False) from exc
        except httpx.TimeoutException as exc:
            raise ConnectorError(f"{self.name} request timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ConnectorError(f"{self.name} transport error: {exc}", retryable=True) from exc
        out = result if isinstance(result, BaseModel) else spec.output_model.model_validate(result)
        dumped: dict[str, Any] = out.model_dump(mode="json")
        return dumped

    async def poll(self, key: str, ctx: ConnectorContext, config: dict[str, Any], state: dict[str, Any]) -> PollResult:
        spec = self.triggers.get(key)
        if spec is None:
            raise ConnectorError(f"Unknown trigger '{key}' for connector '{self.key}'")
        cfg = spec.config_model.model_validate(config)
        handler = getattr(self, spec.handler_name)
        result: PollResult = await handler(ctx, cfg, dict(state))
        return result

    # -- metadata -------------------------------------------------------------------------------

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "key": cls.key,
            "name": cls.name,
            "description": cls.description,
            "category": cls.category,
            "icon": cls.icon,
            "version": cls.version,
            "docs_url": cls.docs_url,
            "auth": {
                "type": cls.auth.type.value,
                "description": cls.auth.description,
                "config_schema": cls.auth.config_model.model_json_schema(),
                "credentials_schema": cls.auth.credentials_model.model_json_schema(),
                "oauth2": (
                    {
                        "authorize_url": cls.auth.oauth2.authorize_url,
                        "token_url": cls.auth.oauth2.token_url,
                        "scopes": cls.auth.oauth2.scopes,
                    }
                    if cls.auth.oauth2
                    else None
                ),
            },
            "actions": [
                {
                    "key": a.key,
                    "name": a.name,
                    "description": a.description,
                    "idempotent": a.idempotent,
                    "capability": a.capability,
                    "input_schema": a.input_model.model_json_schema(),
                    "output_schema": a.output_model.model_json_schema(),
                }
                for a in cls.actions.values()
            ],
            "triggers": [
                {
                    "key": t.key,
                    "name": t.name,
                    "description": t.description,
                    "config_schema": t.config_model.model_json_schema(),
                    "output_schema": t.output_model.model_json_schema() if t.output_model else None,
                    "default_interval_seconds": t.default_interval_seconds,
                }
                for t in cls.triggers.values()
            ],
        }


# ----------------------------------------------------------------------------- HTTP helpers


def raise_for_status(response: httpx.Response, service: str) -> None:
    """Map HTTP failures to ConnectorError with correct retryability."""
    if response.is_success:
        return
    status = response.status_code
    try:
        detail: Any = response.json()
    except ValueError:
        detail = response.text[:500]
    if status == 401:
        raise AuthExpiredError(f"{service}: authentication failed (401)")
    retryable = status in (408, 409, 425, 429) or status >= 500
    message = f"{service} returned HTTP {status}"
    if isinstance(detail, dict):
        msg = detail.get("message") or detail.get("error") or detail.get("errorMessages")
        if msg:
            message += f": {msg}"
    raise ConnectorError(message, retryable=retryable, status_code=status, details=detail)
