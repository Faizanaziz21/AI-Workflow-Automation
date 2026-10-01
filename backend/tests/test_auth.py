from __future__ import annotations

import uuid

from tests.conftest import PASSWORD, add_user, login


async def test_register_and_me(client, tenant):
    r = await client.get("/api/v1/auth/me", headers=tenant.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == tenant.email
    assert "org_admin" in body["roles"]
    assert "workflows:publish" in body["permissions"]


async def test_weak_password_rejected(client):
    r = await client.post(
        "/api/v1/auth/register-org",
        json={
            "organization_name": "Weak Co",
            "email": f"w-{uuid.uuid4().hex[:6]}@example.com",
            "full_name": "W",
            "password": "alllowercaseletters",
        },
    )
    assert r.status_code == 422


async def test_login_wrong_password(client, tenant):
    r = await client.post("/api/v1/auth/login", json={"email": tenant.email, "password": "Wrong-Password-123"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


async def test_account_lockout_after_failed_attempts(client, tenant):
    for _ in range(5):
        await client.post("/api/v1/auth/login", json={"email": tenant.email, "password": "Wrong-Password-123"})
    r = await client.post("/api/v1/auth/login", json={"email": tenant.email, "password": PASSWORD})
    assert r.status_code == 401
    assert "locked" in r.json()["error"]["message"]


async def test_unauthenticated_requests_rejected(client):
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


async def test_refresh_rotation_and_reuse_detection(client, tenant):
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": tenant.refresh_token})
    assert r1.status_code == 200
    new = r1.json()
    assert new["refresh_token"] != tenant.refresh_token
    # Re-using the old token revokes the whole family, including the newly issued token.
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": tenant.refresh_token})
    assert r2.status_code == 401
    r3 = await client.post("/api/v1/auth/refresh", json={"refresh_token": new["refresh_token"]})
    assert r3.status_code == 401
    # Access tokens bound to the revoked family no longer work.
    r4 = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert r4.status_code == 401


async def test_cookie_refresh_requires_csrf(client, tenant):
    tokens = await login(client, tenant.email)
    cookies = {"ff_refresh": tokens["refresh_token"], "ff_csrf": "csrf-value"}
    client.cookies.clear()
    r = await client.post("/api/v1/auth/refresh", cookies=cookies)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"
    r = await client.post("/api/v1/auth/refresh", cookies=cookies, headers={"X-CSRF-Token": "csrf-value"})
    assert r.status_code == 200
    client.cookies.clear()


async def test_logout_revokes_session(client, tenant):
    tokens = await login(client, tenant.email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = await client.post("/api/v1/auth/logout", headers=headers, json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 204
    client.cookies.clear()
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401


async def test_api_key_authentication(client, tenant):
    r = await client.post("/api/v1/api-keys", headers=tenant.headers, json={"name": "ci", "role": "operator"})
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    assert key.startswith("ffk_")
    me = await client.get("/api/v1/auth/me", headers={"X-API-Key": key})
    assert me.status_code == 200
    assert me.json()["actor_type"] == "api_key"
    assert "operator" in me.json()["roles"]
    # Revoke
    await client.delete(f"/api/v1/api-keys/{r.json()['id']}", headers=tenant.headers)
    assert (await client.get("/api/v1/auth/me", headers={"X-API-Key": key})).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers={"X-API-Key": key + "x"})).status_code == 401


async def test_security_headers(client, tenant):
    r = await client.get("/api/v1/auth/me", headers=tenant.headers)
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Request-ID"]


async def test_rate_limiting(client, tenant, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_auth", 3)
    statuses = []
    for _ in range(8):
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "x"},
            headers={"X-Forwarded-For": "203.0.113.77"},
        )
        statuses.append(r.status_code)
    assert 429 in statuses
    limited = next(s for s in statuses if s == 429)
    assert limited == 429


async def test_operator_cannot_manage_users(client, tenant):
    op = await add_user(client, tenant, "operator")
    r = await client.post(
        "/api/v1/users",
        headers=op["headers"],
        json={"email": "x@example.com", "full_name": "X", "password": PASSWORD, "role": "viewer"},
    )
    assert r.status_code == 403
