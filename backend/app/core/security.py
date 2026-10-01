"""Password hashing, JWT issuance/verification, opaque token helpers and HMAC signing."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)

# Pre-computed hash used to equalise timing when the user does not exist.
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_password_strength(password: str) -> None:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        raise ValueError("Password must contain at least three of: lowercase, uppercase, digit, symbol")


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    org_id: uuid.UUID
    session_family: uuid.UUID | None
    expires_at: datetime


def create_access_token(user_id: uuid.UUID, org_id: uuid.UUID, family_id: uuid.UUID | None = None) -> str:
    settings = get_settings()
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org": str(org_id),
        "iat": now,
        "nbf": now,
        "exp": now + settings.access_token_ttl_seconds,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": uuid.uuid4().hex,
        "typ": "access",
    }
    if family_id:
        payload["sfam"] = str(family_id)
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> AccessClaims:
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "sub", "org", "iat"]},
    )
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return AccessClaims(
        user_id=uuid.UUID(payload["sub"]),
        org_id=uuid.UUID(payload["org"]),
        session_family=uuid.UUID(payload["sfam"]) if payload.get("sfam") else None,
        expires_at=datetime.fromtimestamp(payload["exp"], UTC),
    )


def refresh_token_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=get_settings().refresh_token_ttl_seconds)


# ----------------------------------------------------------------------------- webhook signing


def sign_payload(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "v1=" + mac.hexdigest()


def verify_signature(secret: str, header: str | None, timestamp_header: str | None, body: bytes) -> bool:
    """Verify ``X-FlowForge-Signature`` (``v1=<hex>``) over ``timestamp.body`` within tolerance."""
    if not header or not timestamp_header:
        return False
    try:
        ts = int(timestamp_header)
    except ValueError:
        return False
    if abs(time.time() - ts) > get_settings().webhook_signature_tolerance_seconds:
        return False
    expected = sign_payload(secret, ts, body)
    candidates = [part.strip() for part in header.split(",")]
    return any(hmac.compare_digest(expected, c) for c in candidates)
