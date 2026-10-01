from __future__ import annotations

from tests.conftest import PASSWORD, add_user


async def test_workspace_lifecycle_and_isolation(client, make_tenant):
    a = await make_tenant()
    b = await make_tenant()
    r = await client.post("/api/v1/workspaces", headers=a.headers, json={"name": "Sales Ops"})
    assert r.status_code == 201
    ws_id = r.json()["id"]
    assert r.json()["slug"] == "sales-ops"
    # Tenant B can neither see nor modify tenant A's workspace (404, not 403: no existence oracle).
    r = await client.patch(f"/api/v1/workspaces/{ws_id}", headers=b.headers, json={"name": "pwned"})
    assert r.status_code == 404
    names = [w["name"] for w in (await client.get("/api/v1/workspaces", headers=b.headers)).json()]
    assert "Sales Ops" not in names


async def test_workspace_scoped_role_limits_visibility(client, tenant):
    r = await client.post("/api/v1/workspaces", headers=tenant.headers, json={"name": "Finance"})
    finance = r.json()["id"]
    dev = await add_user(client, tenant, "workflow_developer", workspace_id=finance)
    visible = [w["id"] for w in (await client.get("/api/v1/workspaces", headers=dev["headers"])).json()]
    assert visible == [finance]
    me = (await client.get("/api/v1/auth/me", headers=dev["headers"])).json()
    assert me["roles"] == []
    assert me["workspace_roles"][finance] == ["workflow_developer"]


async def test_role_grant_rules(client, tenant):
    dev = await add_user(client, tenant, "workflow_developer")
    # Org admin cannot be granted at workspace scope.
    r = await client.post(
        f"/api/v1/users/{dev['id']}/roles",
        headers=tenant.headers,
        json={"role": "org_admin", "workspace_id": tenant.workspace_id},
    )
    assert r.status_code == 422
    # Org admin cannot mint super admins.
    r = await client.post(f"/api/v1/users/{dev['id']}/roles", headers=tenant.headers, json={"role": "super_admin"})
    assert r.status_code == 403
    # Developer cannot grant anything.
    r = await client.post(f"/api/v1/users/{dev['id']}/roles", headers=dev["headers"], json={"role": "org_admin"})
    assert r.status_code == 403
    r = await client.post(f"/api/v1/users/{dev['id']}/roles", headers=tenant.headers, json={"role": "approver"})
    assert r.status_code == 200
    assert {ra["role"] for ra in r.json()["role_assignments"]} == {"workflow_developer", "approver"}


async def test_cannot_remove_last_org_admin(client, tenant):
    users = (await client.get("/api/v1/users", headers=tenant.headers)).json()["items"]
    me = next(u for u in users if u["email"] == tenant.email)
    ra = me["role_assignments"][0]
    r = await client.delete(f"/api/v1/users/{me['id']}/roles/{ra['id']}", headers=tenant.headers)
    assert r.status_code == 422


async def test_cross_tenant_user_access(client, make_tenant):
    a = await make_tenant()
    b = await make_tenant()
    b_users = (await client.get("/api/v1/users", headers=b.headers)).json()["items"]
    r = await client.patch(f"/api/v1/users/{b_users[0]['id']}", headers=a.headers, json={"is_active": False})
    assert r.status_code == 404
    a_users = (await client.get("/api/v1/users", headers=a.headers)).json()["items"]
    assert all(u["email"] != b.email for u in a_users)


async def test_teams(client, tenant):
    member = await add_user(client, tenant, "approver")
    r = await client.post("/api/v1/teams", headers=tenant.headers, json={"name": "Finance Approvers"})
    assert r.status_code == 201
    team_id = r.json()["id"]
    r = await client.post(f"/api/v1/teams/{team_id}/members", headers=tenant.headers, json={"add": [member["id"]]})
    assert r.json()["member_ids"] == [member["id"]]
    me = (await client.get("/api/v1/auth/me", headers=member["headers"])).json()
    assert team_id in me["team_ids"]


async def test_audit_log_search(client, tenant):
    await client.post("/api/v1/workspaces", headers=tenant.headers, json={"name": "Audited Workspace"})
    r = await client.get("/api/v1/audit-logs", headers=tenant.headers, params={"q": "Audited"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["action"] == "workspace.created" for i in items)
    r = await client.get("/api/v1/audit-logs", headers=tenant.headers, params={"action": "auth.*"})
    assert any(i["action"] == "auth.login" for i in r.json()["items"])
    viewer = await add_user(client, tenant, "viewer")
    assert (await client.get("/api/v1/audit-logs", headers=viewer["headers"])).status_code == 403


async def test_audit_logs_are_tenant_scoped(client, make_tenant):
    a = await make_tenant()
    b = await make_tenant()
    items = (await client.get("/api/v1/audit-logs", headers=a.headers, params={"limit": 500})).json()["items"]
    assert all(b.email != i["actor_email"] for i in items)


async def test_deactivated_user_cannot_login(client, tenant):
    u = await add_user(client, tenant, "viewer")
    await client.patch(f"/api/v1/users/{u['id']}", headers=tenant.headers, json={"is_active": False})
    r = await client.post("/api/v1/auth/login", json={"email": u["email"], "password": PASSWORD})
    assert r.status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=u["headers"])).status_code == 401
