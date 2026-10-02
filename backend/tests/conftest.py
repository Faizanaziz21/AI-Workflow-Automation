from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

os.environ.setdefault("FF_ENV", "test")
os.environ.setdefault("FF_DATABASE_URL", "postgresql+asyncpg://flowforge:flowforge@localhost:5432/flowforge_test")
os.environ.setdefault("FF_REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("FF_RATE_LIMIT_AUTH", "100000")
os.environ.setdefault("FF_RATE_LIMIT_DEFAULT", "100000")
os.environ.setdefault("FF_LOG_JSON", "false")
os.environ.setdefault("FF_LOG_LEVEL", "WARNING")
os.environ.setdefault("FF_SEED_TEMPLATES", "false")
os.environ.setdefault("FF_ALLOW_PRIVATE_NETWORK_EGRESS", "true")
os.environ.setdefault("FF_STORAGE_DIR", "/tmp/flowforge-test-storage")

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND = Path(__file__).resolve().parents[1]
PASSWORD = "Sup3r-Secret-Pass!"


def pytest_sessionstart(session: pytest.Session) -> None:
    """Reset the test database schema and apply migrations once per run."""
    import asyncio

    async def _reset() -> None:
        engine = create_async_engine(os.environ["FF_DATABASE_URL"])
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(_reset())
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, check=True, capture_output=True)


@pytest.fixture(scope="session")
def app():
    from app.main import create_app

    return create_app()


@pytest.fixture(scope="session")
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    from app.core.redis import close_redis
    from app.db.session import dispose_engine

    await close_redis()
    await dispose_engine()


@dataclass
class Tenant:
    org_id: str
    workspace_id: str
    user_id: str
    email: str
    token: str
    refresh_token: str
    extra_users: dict[str, dict] = field(default_factory=dict)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def ws(self, path: str = "") -> str:
        return f"/api/v1/workspaces/{self.workspace_id}{path}"


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> dict:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
async def make_tenant(client):
    async def _make(name: str | None = None) -> Tenant:
        suffix = uuid.uuid4().hex[:10]
        email = f"admin-{suffix}@example.com"
        r = await client.post(
            "/api/v1/auth/register-org",
            json={
                "organization_name": name or f"Org {suffix}",
                "email": email,
                "full_name": "Ada Admin",
                "password": PASSWORD,
            },
        )
        assert r.status_code == 201, r.text
        tokens = r.json()
        me = (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})).json()
        wss = (
            await client.get("/api/v1/workspaces", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        ).json()
        return Tenant(
            org_id=me["organization"]["id"],
            workspace_id=wss[0]["id"],
            user_id=me["user"]["id"],
            email=email,
            token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
        )

    return _make


@pytest.fixture
async def tenant(make_tenant) -> Tenant:
    return await make_tenant()


async def add_user(client: httpx.AsyncClient, tenant: Tenant, role: str, workspace_id: str | None = None) -> dict:
    email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/api/v1/users",
        headers=tenant.headers,
        json={
            "email": email,
            "full_name": role.title(),
            "password": PASSWORD,
            "role": role,
            "workspace_id": workspace_id,
        },
    )
    assert r.status_code == 201, r.text
    tokens = await login(client, email)
    return {"id": r.json()["id"], "email": email, "headers": {"Authorization": f"Bearer {tokens['access_token']}"}}


# ----------------------------------------------------------------------------- engine helpers


async def drain(max_seconds: float = 20.0) -> None:
    """Process queued jobs in-process until the queue is idle (only jobs already due)."""
    import asyncio

    from app.workers.worker import Worker

    worker = Worker(concurrency=8, worker_id="test-worker", grace_seconds=5)
    await asyncio.wait_for(worker.run(stop_when_idle=True, idle_timeout=0.3), timeout=max_seconds)


async def fast_forward(execution_id: str | None = None) -> None:
    """Make delayed jobs (timers, retries) due now."""
    from sqlalchemy import text

    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        if execution_id:
            await s.execute(
                text(
                    "UPDATE jobs SET available_at = now() WHERE status='queued' AND execution_id = :e "
                    "AND kind <> 'execution.timeout'"
                ),
                {"e": execution_id},
            )
        else:
            await s.execute(
                text("UPDATE jobs SET available_at = now() WHERE status='queued' AND kind <> 'execution.timeout'")
            )
        await s.commit()


async def publish(client, tenant, definition: dict, name: str = "wf") -> str:
    r = await client.post(
        tenant.ws("/workflows"), headers=tenant.headers, json={"name": name, "definition": definition}
    )
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]
    r = await client.post(tenant.ws(f"/workflows/{wf_id}/publish"), headers=tenant.headers, json={})
    assert r.status_code == 200, r.text
    return wf_id


async def run(client, tenant, wf_id: str, payload: dict | None = None, headers: dict | None = None, **extra) -> str:
    r = await client.post(
        tenant.ws(f"/workflows/{wf_id}/run"), headers=headers or tenant.headers, json={"input": payload or {}, **extra}
    )
    assert r.status_code == 202, r.text
    return r.json()["execution_id"]


async def get_execution(client, tenant, ex_id: str) -> dict:
    r = await client.get(f"/api/v1/executions/{ex_id}", headers=tenant.headers)
    assert r.status_code == 200, r.text
    return r.json()


async def node_runs(client, tenant, ex_id: str) -> dict[str, dict]:
    r = await client.get(f"/api/v1/executions/{ex_id}/nodes", headers=tenant.headers)
    assert r.status_code == 200, r.text
    return {(n["node_id"] + (f"@{n['scope']}" if n["scope"] else "")): n for n in r.json()}
