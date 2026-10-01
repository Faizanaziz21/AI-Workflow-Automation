from __future__ import annotations

import time
import uuid

import pytest
from cryptography.exceptions import InvalidTag

from app.core import crypto
from app.core.masking import MASK, mask_data, mask_inline
from app.core.rbac import Permission, can_grant, permissions_for
from app.core.security import sign_payload, verify_signature
from app.db.enums import Role


def test_envelope_encryption_roundtrip_and_aad_binding():
    org, sid = str(uuid.uuid4()), str(uuid.uuid4())
    blob = crypto.encrypt_json({"api_key": "sk-test-123"}, org_id=org, secret_id=sid)
    assert b"sk-test-123" not in blob.ciphertext
    assert crypto.decrypt_json(blob, org_id=org, secret_id=sid) == {"api_key": "sk-test-123"}
    with pytest.raises(InvalidTag):
        crypto.decrypt_json(blob, org_id=str(uuid.uuid4()), secret_id=sid)


def test_kek_rotation_rewrap():
    import base64
    import os

    old = os.urandom(32)
    new = os.urandom(32)
    crypto.set_key_provider(crypto.StaticKeyProvider({"v1": old}, "v1"))
    try:
        blob = crypto.encrypt_json({"password": "hunter2"}, org_id="o", secret_id="s")
        crypto.set_key_provider(crypto.StaticKeyProvider({"v1": old, "v2": new}, "v2"))
        rewrapped = crypto.rewrap(blob, org_id="o", secret_id="s")
        assert rewrapped.kek_version == "v2"
        assert rewrapped.ciphertext == blob.ciphertext
        crypto.set_key_provider(crypto.StaticKeyProvider({"v2": new}, "v2"))
        assert crypto.decrypt_json(rewrapped, org_id="o", secret_id="s") == {"password": "hunter2"}
        assert base64.b64encode(new)
    finally:
        crypto.set_key_provider(None)


def test_masking_by_key_and_value():
    data = {
        "headers": {"Authorization": "Bearer abc.def.ghi", "X-Trace": "1"},
        "body": {"message": "token is s3cr3t-value here", "password": "pw"},
        "usage": {"input_tokens": 12},
    }
    masked = mask_data(data, ["s3cr3t-value"])
    assert masked["headers"]["Authorization"] == MASK
    assert masked["headers"]["X-Trace"] == "1"
    assert masked["body"]["password"] == MASK
    assert "s3cr3t-value" not in masked["body"]["message"]
    assert masked["usage"]["input_tokens"] == 12


def test_inline_masking_patterns():
    s = "call with sk-abcdefghijklmnopqrstuvwx and postgresql://app:pa55@db:5432/x and Bearer xyz123"
    out = mask_inline(s)
    assert "sk-abcdefghijklmnopqrstuvwx" not in out
    assert "pa55" not in out
    assert "xyz123" not in out
    assert "postgresql://app:" in out


def test_webhook_signature():
    body = b'{"a":1}'
    ts = int(time.time())
    sig = sign_payload("whsec", ts, body)
    assert verify_signature("whsec", sig, str(ts), body)
    assert not verify_signature("whsec", sig, str(ts), b'{"a":2}')
    assert not verify_signature("other", sig, str(ts), body)
    old = ts - 3600
    assert not verify_signature("whsec", sign_payload("whsec", old, body), str(old), body)


def test_rbac_matrix():
    assert Permission.APPROVALS_DECIDE in permissions_for({Role.APPROVER})
    assert Permission.WORKFLOWS_WRITE not in permissions_for({Role.OPERATOR})
    assert Permission.EXECUTIONS_OPERATE in permissions_for({Role.OPERATOR})
    assert Permission.WORKFLOWS_READ in permissions_for({Role.VIEWER})
    assert Permission.EXECUTIONS_RUN not in permissions_for({Role.VIEWER})
    assert Permission.AUDIT_READ not in permissions_for({Role.WORKFLOW_DEVELOPER})
    assert can_grant({Role.ORG_ADMIN}, Role.WORKFLOW_DEVELOPER)
    assert not can_grant({Role.ORG_ADMIN}, Role.SUPER_ADMIN)
    assert not can_grant({Role.WORKFLOW_DEVELOPER}, Role.VIEWER)


def test_settings_parse_list_env(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("FF_CORS_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("FF_EGRESS_ALLOWLIST", '["*.corp.internal"]')
    s = Settings()
    assert s.cors_origins == ["https://a.example", "https://b.example"]
    assert s.egress_allowlist == ["*.corp.internal"]
