"""Envelope encryption (AES-256-GCM) with versioned key-encryption keys.

* A fresh 256-bit data-encryption key (DEK) encrypts each secret payload.
* The DEK is wrapped with the active KEK (also AES-256-GCM) and stored alongside the ciphertext.
* Rotating the KEK only re-wraps DEKs (``rewrap``); payload ciphertext is untouched.
* Associated data binds ciphertext to its owning org + secret id, so a ciphertext copied to another
  tenant's row fails authentication.

The ``KeyProvider`` interface lets production deployments plug in a KMS (AWS KMS, GCP KMS, Vault Transit)
without changing callers.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings


class KeyProvider(Protocol):
    active_version: str

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]: ...

    def unwrap(self, version: str, wrapped: bytes, aad: bytes) -> bytes: ...


class StaticKeyProvider:
    """KEKs from configuration (``FF_ENCRYPTION_KEYS``)."""

    def __init__(self, keys: dict[str, bytes], active_version: str) -> None:
        self._keys = keys
        self.active_version = active_version

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(12)
        wrapped = nonce + AESGCM(self._keys[self.active_version]).encrypt(nonce, dek, aad)
        return self.active_version, wrapped

    def unwrap(self, version: str, wrapped: bytes, aad: bytes) -> bytes:
        if version not in self._keys:
            raise KeyError(f"Unknown key-encryption-key version {version!r}")
        return AESGCM(self._keys[version]).decrypt(wrapped[:12], wrapped[12:], aad)


@dataclass(frozen=True)
class EncryptedBlob:
    kek_version: str
    wrapped_dek: bytes
    nonce: bytes
    ciphertext: bytes
    fingerprint: str


_provider: KeyProvider | None = None


def get_key_provider() -> KeyProvider:
    global _provider
    if _provider is None:
        s = get_settings()
        _provider = StaticKeyProvider(s.kek_map(), s.active_encryption_key)
    return _provider


def set_key_provider(provider: KeyProvider | None) -> None:
    global _provider
    _provider = provider


def _aad(org_id: str, secret_id: str) -> bytes:
    return f"flowforge:{org_id}:{secret_id}".encode()


def encrypt_json(data: dict[str, Any], *, org_id: str, secret_id: str) -> EncryptedBlob:
    provider = get_key_provider()
    aad = _aad(org_id, secret_id)
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    plaintext = json.dumps(data, sort_keys=True).encode()
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    version, wrapped = provider.wrap(dek, aad)
    return EncryptedBlob(
        kek_version=version,
        wrapped_dek=wrapped,
        nonce=nonce,
        ciphertext=ciphertext,
        fingerprint=hashlib.sha256(plaintext).hexdigest()[:16],
    )


def decrypt_json(blob: EncryptedBlob, *, org_id: str, secret_id: str) -> dict[str, Any]:
    aad = _aad(org_id, secret_id)
    dek = get_key_provider().unwrap(blob.kek_version, blob.wrapped_dek, aad)
    plaintext = AESGCM(dek).decrypt(blob.nonce, blob.ciphertext, aad)
    result: dict[str, Any] = json.loads(plaintext)
    return result


def rewrap(blob: EncryptedBlob, *, org_id: str, secret_id: str) -> EncryptedBlob:
    """Re-wrap the DEK under the currently active KEK (KEK rotation)."""
    provider = get_key_provider()
    aad = _aad(org_id, secret_id)
    dek = provider.unwrap(blob.kek_version, blob.wrapped_dek, aad)
    version, wrapped = provider.wrap(dek, aad)
    return EncryptedBlob(version, wrapped, blob.nonce, blob.ciphertext, blob.fingerprint)
