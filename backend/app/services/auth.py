"""Authentication: organisation sign-up, login, refresh-token rotation, API keys, principal loading."""

from __future__ import annotations

import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import get_settings
from app.core.errors import AuthenticationError, ConflictError, ValidationFailedError
from app.core.principal import Principal
from app.db.base import uuid7
from app.db.enums import ActorType, Role
from app.db.models import ApiKey, Organization, RefreshToken, RoleAssignment, TeamMember, User, Workspace
from app.services import audit

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
API_KEY_PREFIX = "ffk_"


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "bearer"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:60] or "org"


async def register_organization(
    session: AsyncSession, *, org_name: str, email: str, full_name: str, password: str
) -> tuple[Organization, User]:
    try:
        security.validate_password_strength(password)
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from exc
    email = email.lower().strip()
    if (await session.execute(select(User.id).where(User.email == email))).first():
        raise ConflictError("A user with this email already exists")
    base_slug = slugify(org_name)
    slug = base_slug
    suffix = 1
    while (await session.execute(select(Organization.id).where(Organization.slug == slug))).first():
        suffix += 1
        slug = f"{base_slug}-{suffix}"
    org = Organization(id=uuid7(), name=org_name, slug=slug, settings={})
    session.add(org)
    await session.flush()
    ws = Workspace(org_id=org.id, name="Default", slug="default", description="Default workspace")
    user = User(
        id=uuid7(),
        org_id=org.id,
        email=email,
        full_name=full_name,
        password_hash=security.hash_password(password),
    )
    session.add_all([ws, user])
    await session.flush()
    session.add(RoleAssignment(org_id=org.id, user_id=user.id, role=Role.ORG_ADMIN.value))
    await audit.record(
        session,
        None,
        "org.created",
        org_id=org.id,
        resource_type="organization",
        resource_id=org.id,
        summary=f"Organization {org_name} created by {email}",
        actor_email=email,
    )
    return org, user


async def _issue_tokens(
    session: AsyncSession,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[TokenPair, RefreshToken]:
    family = family_id or uuid7()
    raw_refresh = security.generate_token(48)
    record = RefreshToken(
        id=uuid7(),
        user_id=user.id,
        org_id=user.org_id,
        token_hash=security.sha256_hex(raw_refresh),
        family_id=family,
        expires_at=security.refresh_token_expiry(),
        user_agent=(user_agent or "")[:500] or None,
        ip_address=ip,
    )
    session.add(record)
    pair = TokenPair(
        access_token=security.create_access_token(user.id, user.org_id, family),
        refresh_token=raw_refresh,
        expires_in=get_settings().access_token_ttl_seconds,
    )
    return pair, record


async def login(
    session: AsyncSession, *, email: str, password: str, user_agent: str | None = None, ip: str | None = None
) -> TokenPair:
    email = email.lower().strip()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    now = datetime.now(UTC)
    if user and user.locked_until and user.locked_until > now:
        await audit.record(
            session,
            None,
            "auth.login",
            org_id=user.org_id,
            outcome="denied",
            summary="Login attempt on locked account",
            actor_email=email,
        )
        await session.commit()
        raise AuthenticationError("Account temporarily locked due to repeated failed logins")
    if not security.verify_password(password, user.password_hash if user else None) or not user or not user.is_active:
        if user:
            user.failed_login_count += 1
            if user.failed_login_count >= MAX_FAILED_LOGINS:
                user.locked_until = now + LOCKOUT
                user.failed_login_count = 0
            await audit.record(
                session,
                None,
                "auth.login",
                org_id=user.org_id,
                outcome="failure",
                summary="Invalid credentials",
                actor_email=email,
            )
            await session.commit()
        raise AuthenticationError("Invalid email or password")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    pair, _ = await _issue_tokens(session, user, user_agent=user_agent, ip=ip)
    principal = Principal(actor_type=ActorType.USER, org_id=user.org_id, user_id=user.id, email=user.email)
    await audit.record(
        session, principal, "auth.login", resource_type="user", resource_id=user.id, summary=f"{email} logged in"
    )
    return pair


async def refresh(
    session: AsyncSession, raw_token: str, *, user_agent: str | None = None, ip: str | None = None
) -> TokenPair:
    """Rotate a refresh token. Re-use of a rotated token revokes the whole token family."""
    token_hash = security.sha256_hex(raw_token)
    record = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update())
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if record is None:
        raise AuthenticationError("Invalid refresh token")
    if record.revoked_at is not None:
        # Token reuse => likely theft. Kill the whole family.
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == record.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await audit.record(
            session,
            None,
            "auth.refresh_reuse_detected",
            org_id=record.org_id,
            outcome="denied",
            resource_type="user",
            resource_id=record.user_id,
            summary="Refresh token reuse detected; session family revoked",
        )
        await session.commit()
        raise AuthenticationError("Refresh token reuse detected; please sign in again")
    if record.expires_at <= now:
        raise AuthenticationError("Refresh token expired")
    user = await session.get(User, record.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("User inactive")
    pair, new_record = await _issue_tokens(session, user, family_id=record.family_id, user_agent=user_agent, ip=ip)
    record.revoked_at = now
    record.replaced_by = new_record.id
    return pair


async def logout(session: AsyncSession, principal: Principal, raw_refresh_token: str | None) -> None:
    now = datetime.now(UTC)
    if raw_refresh_token:
        record = (
            await session.execute(
                select(RefreshToken).where(RefreshToken.token_hash == security.sha256_hex(raw_refresh_token))
            )
        ).scalar_one_or_none()
        if record and record.user_id == principal.user_id:
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == record.family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
    await audit.record(session, principal, "auth.logout", resource_type="user", resource_id=principal.user_id)


async def is_session_family_active(session: AsyncSession, family_id: uuid.UUID) -> bool:
    row = (
        await session.execute(
            select(RefreshToken.id)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .limit(1)
        )
    ).first()
    return row is not None


async def load_user_principal(session: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID) -> Principal:
    user = await session.get(User, user_id)
    if user is None or not user.is_active or user.org_id != org_id:
        raise AuthenticationError("User not found or inactive")
    org = await session.get(Organization, org_id)
    if org is None or not org.is_active:
        raise AuthenticationError("Organization inactive")
    org_roles: set[Role] = set()
    ws_roles: dict[uuid.UUID, set[Role]] = {}
    for ra in user.role_assignments:
        role = Role(ra.role)
        if ra.workspace_id is None:
            org_roles.add(role)
        else:
            ws_roles.setdefault(ra.workspace_id, set()).add(role)
    team_ids = set(
        (await session.execute(select(TeamMember.team_id).where(TeamMember.user_id == user.id))).scalars().all()
    )
    return Principal(
        actor_type=ActorType.USER,
        org_id=user.org_id,
        user_id=user.id,
        email=user.email,
        name=user.full_name,
        is_super_admin=user.is_super_admin,
        org_roles=org_roles,
        workspace_roles=ws_roles,
        team_ids=team_ids,
    )


# ----------------------------------------------------------------------------- API keys


async def create_api_key(
    session: AsyncSession,
    principal: Principal,
    *,
    name: str,
    role: Role,
    workspace_id: uuid.UUID | None,
    expires_at: datetime | None,
) -> tuple[ApiKey, str]:
    prefix = security.generate_token(6).replace("-", "a").replace("_", "b")[:8]
    secret = security.generate_token(32)
    raw = f"{API_KEY_PREFIX}{prefix}_{secret}"
    key = ApiKey(
        org_id=principal.org_id,
        workspace_id=workspace_id,
        name=name,
        prefix=prefix,
        key_hash=security.sha256_hex(raw),
        role=role.value,
        created_by=principal.user_id,
        expires_at=expires_at,
    )
    session.add(key)
    await session.flush()
    await audit.record(
        session,
        principal,
        "api_key.created",
        resource_type="api_key",
        resource_id=key.id,
        workspace_id=workspace_id,
        summary=f"API key '{name}' created with role {role.value}",
    )
    return key, raw


async def authenticate_api_key(session: AsyncSession, raw: str) -> Principal:
    if not raw.startswith(API_KEY_PREFIX) or raw.count("_") < 2:
        raise AuthenticationError("Invalid API key")
    prefix = raw[len(API_KEY_PREFIX) :].split("_", 1)[0]
    key = (await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))).scalar_one_or_none()
    now = datetime.now(UTC)
    if key is None or not hmac.compare_digest(key.key_hash, security.sha256_hex(raw)):
        raise AuthenticationError("Invalid API key")
    if key.revoked_at is not None or (key.expires_at and key.expires_at <= now):
        raise AuthenticationError("API key revoked or expired")
    org = await session.get(Organization, key.org_id)
    if org is None or not org.is_active:
        raise AuthenticationError("Organization inactive")
    if key.last_used_at is None or (now - key.last_used_at) > timedelta(minutes=1):
        key.last_used_at = now
    role = Role(key.role)
    principal = Principal(
        actor_type=ActorType.API_KEY,
        org_id=key.org_id,
        api_key_id=key.id,
        name=key.name,
    )
    if key.workspace_id is None:
        principal.org_roles = {role}
    else:
        principal.workspace_roles = {key.workspace_id: {role}}
    return principal
