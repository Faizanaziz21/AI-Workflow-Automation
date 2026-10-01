"""Connections: encrypted credential storage, access policies, testing, rotation and runtime resolution."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.registry import get_connector
from app.connectors.sdk import AuthType, Connector, ConnectorContext, ConnectorError, TestResult
from app.core import crypto
from app.core.config import get_settings
from app.core.egress import get_http_client
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from app.core.masking import MASK
from app.core.principal import Principal
from app.core.rbac import Permission
from app.core.redis import get_redis
from app.db.models import Connection, Secret
from app.db.session import get_sessionmaker
from app.services import audit
from app.services.storage import PlatformFileStore


def _blob(secret: Secret) -> crypto.EncryptedBlob:
    return crypto.EncryptedBlob(
        secret.kek_version, secret.wrapped_dek, secret.nonce, secret.ciphertext, secret.fingerprint
    )


def _validation_error(prefix: str, exc: ValidationError) -> ValidationFailedError:
    # Never echo submitted values (they may be secrets) — only locations and messages.
    details = [{"loc": [prefix, *e["loc"]], "msg": e["msg"]} for e in exc.errors()]
    return ValidationFailedError(f"Invalid {prefix}", details=details)


def validate_config(connector: Connector, config: dict[str, Any]) -> BaseModel:
    try:
        return connector.auth.config_model.model_validate(config)
    except ValidationError as exc:
        raise _validation_error("config", exc) from exc


def validate_credentials(connector: Connector, creds: dict[str, Any]) -> BaseModel:
    try:
        return connector.auth.credentials_model.model_validate(creds)
    except ValidationError as exc:
        raise _validation_error("credentials", exc) from exc


async def write_secret(
    session: AsyncSession, org_id: uuid.UUID, payload: dict[str, Any], existing: Secret | None = None
) -> Secret:
    secret = existing or Secret(
        id=uuid.uuid4(), org_id=org_id, kek_version="", wrapped_dek=b"", nonce=b"", ciphertext=b"", fingerprint=""
    )
    blob = crypto.encrypt_json(payload, org_id=str(org_id), secret_id=str(secret.id))
    secret.kek_version = blob.kek_version
    secret.wrapped_dek = blob.wrapped_dek
    secret.nonce = blob.nonce
    secret.ciphertext = blob.ciphertext
    secret.fingerprint = blob.fingerprint
    if existing is not None:
        secret.version += 1
        secret.rotated_at = datetime.now(UTC)
    else:
        session.add(secret)
    await session.flush()
    return secret


async def read_secret(session: AsyncSession, secret_id: uuid.UUID | None, org_id: uuid.UUID) -> dict[str, Any]:
    if secret_id is None:
        return {}
    secret = (
        await session.execute(select(Secret).where(Secret.id == secret_id, Secret.org_id == org_id))
    ).scalar_one_or_none()
    if secret is None:
        return {}
    return crypto.decrypt_json(_blob(secret), org_id=str(org_id), secret_id=str(secret.id))


def credential_fields_set(creds: dict[str, Any]) -> list[str]:
    return sorted(k for k, v in creds.items() if v not in (None, ""))


def can_use_connection(principal: Principal, conn: Connection) -> bool:
    if not principal.has(Permission.CONNECTIONS_USE, conn.workspace_id):
        return False
    policy = conn.access_policy or {}
    roles = set(policy.get("roles") or [])
    teams = {str(t) for t in policy.get("team_ids") or []}
    if roles and not roles & {r.value for r in principal.roles_for(conn.workspace_id)}:
        return principal.has(Permission.CONNECTIONS_MANAGE, conn.workspace_id)
    if teams and not teams & {str(t) for t in principal.team_ids}:
        return principal.has(Permission.CONNECTIONS_MANAGE, conn.workspace_id)
    return True


async def get_connection(
    session: AsyncSession, org_id: uuid.UUID, connection_id: uuid.UUID, workspace_id: uuid.UUID | None = None
) -> Connection:
    stmt = select(Connection).where(Connection.id == connection_id, Connection.org_id == org_id)
    if workspace_id is not None:
        stmt = stmt.where(Connection.workspace_id == workspace_id)
    conn = (await session.execute(stmt)).scalar_one_or_none()
    if conn is None:
        raise NotFoundError("Connection not found")
    return conn


async def create_connection(
    session: AsyncSession,
    principal: Principal,
    *,
    workspace_id: uuid.UUID,
    name: str,
    connector_key: str,
    config: dict[str, Any],
    credentials: dict[str, Any],
    access_policy: dict[str, Any],
) -> Connection:
    try:
        connector = get_connector(connector_key)
    except KeyError as exc:
        raise ValidationFailedError(f"Unknown connector '{connector_key}'") from exc
    validate_config(connector, config)
    if connector.auth.type != AuthType.OAUTH2 or credentials:
        validate_credentials(connector, credentials)
    conn = Connection(
        id=uuid.uuid4(),
        org_id=principal.org_id,
        workspace_id=workspace_id,
        name=name,
        connector_key=connector_key,
        auth_type=connector.auth.type.value,
        config=validate_config(connector, config).model_dump(mode="json"),
        access_policy=access_policy,
        created_by=principal.user_id,
        status="pending_authorization"
        if connector.auth.type == AuthType.OAUTH2 and not credentials.get("refresh_token")
        else "untested",
    )
    if credentials:
        secret = await write_secret(session, principal.org_id, credentials)
        conn.secret_id = secret.id
    session.add(conn)
    await session.flush()
    await audit.record(
        session,
        principal,
        "connection.created",
        resource_type="connection",
        resource_id=conn.id,
        workspace_id=workspace_id,
        summary=f"Connection '{name}' ({connector_key}) created",
        details={"credential_fields": credential_fields_set(credentials)},
    )
    return conn


async def update_connection(
    session: AsyncSession,
    principal: Principal,
    conn: Connection,
    *,
    name: str | None = None,
    config: dict[str, Any] | None = None,
    credentials: dict[str, Any] | None = None,
    access_policy: dict[str, Any] | None = None,
) -> Connection:
    connector = get_connector(conn.connector_key)
    changed: list[str] = []
    if name is not None and name != conn.name:
        conn.name = name
        changed.append("name")
    if config is not None:
        conn.config = validate_config(connector, {**conn.config, **config}).model_dump(mode="json")
        changed.append("config")
    if access_policy is not None:
        conn.access_policy = access_policy
        changed.append("access_policy")
    submitted = {k: v for k, v in (credentials or {}).items() if v != MASK}
    if submitted:
        current = await read_secret(session, conn.secret_id, conn.org_id)
        merged = {**current, **submitted}
        validate_credentials(connector, merged)
        existing = await session.get(Secret, conn.secret_id) if conn.secret_id else None
        secret = await write_secret(session, conn.org_id, merged, existing)
        conn.secret_id = secret.id
        conn.status = "untested"
        changed.append("credentials")
    action = "connection.credentials_rotated" if changed == ["credentials"] else "connection.updated"
    await audit.record(
        session,
        principal,
        action,
        resource_type="connection",
        resource_id=conn.id,
        workspace_id=conn.workspace_id,
        summary=f"Connection '{conn.name}' updated",
        details={"fields": changed},
    )
    return conn


async def delete_connection(session: AsyncSession, principal: Principal, conn: Connection) -> None:
    secret = await session.get(Secret, conn.secret_id) if conn.secret_id else None
    await session.delete(conn)
    if secret:
        await session.delete(secret)
    await audit.record(
        session,
        principal,
        "connection.deleted",
        resource_type="connection",
        resource_id=conn.id,
        workspace_id=conn.workspace_id,
        summary=f"Connection '{conn.name}' deleted",
    )


# ----------------------------------------------------------------------------- runtime resolution


@dataclass
class ResolvedConnection:
    connection: Connection
    connector: Connector
    context: ConnectorContext
    secret_values: list[str]


def _secret_strings(data: Any) -> list[str]:
    out: list[str] = []
    if isinstance(data, dict):
        for v in data.values():
            out.extend(_secret_strings(v))
    elif isinstance(data, list):
        for v in data:
            out.extend(_secret_strings(v))
    elif isinstance(data, str) and len(data) >= 6:
        out.append(data)
    return out


async def resolve(
    session: AsyncSession,
    org_id: uuid.UUID,
    connection_id: uuid.UUID | str,
    *,
    workspace_id: uuid.UUID,
    log: Any = None,
    idempotency_key: str | None = None,
    timeout: float = 30.0,
) -> ResolvedConnection:
    """Load + decrypt a connection for execution. Connections never cross workspace boundaries."""
    try:
        cid = uuid.UUID(str(connection_id))
    except ValueError as exc:
        raise ConnectorError(f"Invalid connection id {connection_id!r}") from exc
    conn = (
        await session.execute(select(Connection).where(Connection.id == cid, Connection.org_id == org_id))
    ).scalar_one_or_none()
    if conn is None or conn.workspace_id != workspace_id:
        raise ConnectorError("Connection not found in this workspace")
    connector = get_connector(conn.connector_key)
    raw_creds = await read_secret(session, conn.secret_id, org_id)
    config = connector.auth.config_model.model_validate(conn.config)
    try:
        creds = connector.auth.credentials_model.model_validate(raw_creds)
    except ValidationError as exc:
        raise ConnectorError(f"Connection '{conn.name}' has incomplete credentials; reconfigure it") from exc
    sessionmaker = get_sessionmaker()

    async def save_credentials(new: dict[str, Any]) -> None:
        async with sessionmaker() as s:
            fresh = await s.get(Connection, conn.id)
            if fresh is None:
                return
            existing = await s.get(Secret, fresh.secret_id) if fresh.secret_id else None
            secret = await write_secret(s, org_id, new, existing)
            fresh.secret_id = secret.id
            if isinstance(new.get("expires_at"), (int, float)):
                fresh.credentials_expire_at = datetime.fromtimestamp(new["expires_at"], UTC)
            await audit.record(
                s,
                None,
                "connection.credentials_refreshed",
                org_id=org_id,
                resource_type="connection",
                resource_id=conn.id,
                workspace_id=conn.workspace_id,
                summary=f"Credentials for '{conn.name}' refreshed automatically",
            )
            await s.commit()

    ctx = ConnectorContext(
        org_id=str(org_id),
        connection_id=str(conn.id),
        config=config,
        credentials=creds,
        http=get_http_client(),
        log=log or (lambda level, msg, data=None: None),
        idempotency_key=idempotency_key,
        files=PlatformFileStore(sessionmaker, org_id, conn.workspace_id),
        save_credentials=save_credentials,
        timeout=timeout,
    )
    return ResolvedConnection(conn, connector, ctx, _secret_strings(raw_creds))


async def run_connection_test(session: AsyncSession, principal: Principal, conn: Connection) -> TestResult:
    try:
        resolved = await resolve(session, conn.org_id, conn.id, workspace_id=conn.workspace_id, timeout=15)
        result = await resolved.connector.test_connection(resolved.context)
    except ConnectorError as exc:
        result = TestResult(False, exc.message)
    except Exception as exc:  # surface any connector bug as a failed test, not a 500
        result = TestResult(False, f"{type(exc).__name__}: {exc}")
    conn.status = "connected" if result.ok else "error"
    conn.status_message = result.message[:2000]
    conn.last_tested_at = datetime.now(UTC)
    await audit.record(
        session,
        principal,
        "connection.tested",
        resource_type="connection",
        resource_id=conn.id,
        workspace_id=conn.workspace_id,
        outcome="success" if result.ok else "failure",
        summary=f"Connection '{conn.name}' test: {result.message[:200]}",
    )
    return result


async def rewrap_org_secrets(session: AsyncSession, principal: Principal) -> int:
    """KEK rotation: re-wrap every data key of the organisation under the active KEK."""
    rows = (await session.execute(select(Secret).where(Secret.org_id == principal.org_id))).scalars().all()
    active = crypto.get_key_provider().active_version
    count = 0
    for secret in rows:
        if secret.kek_version == active:
            continue
        blob = crypto.rewrap(_blob(secret), org_id=str(secret.org_id), secret_id=str(secret.id))
        secret.kek_version = blob.kek_version
        secret.wrapped_dek = blob.wrapped_dek
        count += 1
    await audit.record(
        session,
        principal,
        "secrets.rewrapped",
        resource_type="organization",
        resource_id=principal.org_id,
        summary=f"Re-wrapped {count} secrets under KEK {active}",
    )
    return count


# ----------------------------------------------------------------------------- OAuth2 authorization code + PKCE


def oauth_redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + "/api/v1/connections/oauth/callback"


async def oauth_start(session: AsyncSession, principal: Principal, conn: Connection) -> str:
    connector = get_connector(conn.connector_key)
    spec = connector.auth.oauth2
    if spec is None:
        raise ValidationFailedError("Connector does not use OAuth2")
    if not can_use_connection(principal, conn) or not principal.has(Permission.CONNECTIONS_MANAGE, conn.workspace_id):
        raise PermissionDeniedError("Not allowed to authorize this connection")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    await get_redis().set(
        f"oauth:{state}",
        json.dumps(
            {
                "connection_id": str(conn.id),
                "org_id": str(conn.org_id),
                "verifier": verifier,
                "user_id": str(principal.user_id) if principal.user_id else None,
            }
        ),
        ex=600,
    )
    params = {
        "client_id": conn.config.get("client_id", ""),
        "redirect_uri": oauth_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(spec.scopes),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    if spec.pkce:
        params.update({"code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{spec.authorize_url}?{urlencode(params)}"


async def oauth_callback(session: AsyncSession, state: str, code: str) -> Connection:
    raw = await get_redis().getdel(f"oauth:{state}")
    if not raw:
        raise ValidationFailedError("OAuth state expired or invalid")
    data = json.loads(raw)
    org_id = uuid.UUID(data["org_id"])
    conn = await get_connection(session, org_id, uuid.UUID(data["connection_id"]))
    connector = get_connector(conn.connector_key)
    creds_raw = await read_secret(session, conn.secret_id, org_id)
    ctx = ConnectorContext(
        org_id=str(org_id),
        connection_id=str(conn.id),
        config=connector.auth.config_model.model_validate(conn.config),
        credentials=connector.auth.credentials_model.model_validate(creds_raw),
        http=get_http_client(),
    )
    exchange = getattr(connector, "exchange_code", None)
    if exchange is None:
        raise ValidationFailedError("Connector does not support authorization-code exchange")
    new_creds = await exchange(ctx, code, oauth_redirect_uri(), data["verifier"])
    existing = await session.get(Secret, conn.secret_id) if conn.secret_id else None
    secret = await write_secret(session, org_id, new_creds, existing)
    conn.secret_id = secret.id
    conn.status = "connected"
    conn.status_message = "Authorized via OAuth"
    await audit.record(
        session,
        None,
        "connection.oauth_authorized",
        org_id=org_id,
        resource_type="connection",
        resource_id=conn.id,
        workspace_id=conn.workspace_id,
        summary=f"Connection '{conn.name}' authorized via OAuth",
    )
    return conn
