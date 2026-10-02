from __future__ import annotations

from tests.conftest import add_user, drain, fast_forward, get_execution, node_runs, publish, run
from tests.factories import definition, edge, node


def approval_flow(**approval_cfg) -> dict:
    cfg = {
        "title": "Refund of ${{ trigger.amount }}",
        "description": "AI recommends a refund",
        "context": {"amount": "{{ trigger.amount }}", "customer": "{{ trigger.customer }}"},
        "approvers": {"role": "approver"},
        "due_in_hours": 24,
        **approval_cfg,
    }
    return definition(
        [
            node("t", "trigger.manual"),
            node("check", "logic.if", {"condition": "{{ trigger.amount > 5000 }}"}),
            node("approval", "human.approval", cfg),
            node(
                "refund",
                "logic.set_variable",
                {"assignments": {"refunded": "{{ trigger.amount }}", "by": "{{ nodes.approval.output.decided_by }}"}},
            ),
            node("auto_refund", "logic.set_variable", {"assignments": {"refunded": "{{ trigger.amount }}"}}),
            node("deny", "logic.set_variable", {"assignments": {"reason": "{{ nodes.approval.output.comment }}"}}),
        ],
        [
            edge("t", "check"),
            edge("check", "approval", "true"),
            edge("check", "auto_refund", "false"),
            edge("approval", "refund", "approved"),
            edge("approval", "deny", "rejected"),
        ],
    )


async def test_refund_over_threshold_requires_approval(client, tenant):
    approver = await add_user(client, tenant, "approver")
    viewer = await add_user(client, tenant, "viewer")
    wf = await publish(client, tenant, approval_flow())
    ex_id = await run(client, tenant, wf, {"amount": 8000, "customer": "Acme"})
    small = await run(client, tenant, wf, {"amount": 100, "customer": "Tiny"})
    await drain()
    assert (await get_execution(client, tenant, small))["status"] == "COMPLETED"
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "WAITING_APPROVAL"
    inbox = (await client.get("/api/v1/approvals", headers=approver["headers"])).json()
    item = next(i for i in inbox["items"] if i["execution_id"] == ex_id)
    assert item["title"] == "Refund of $8000" and item["context"] == {"amount": 8000, "customer": "Acme"}
    assert item["can_decide"] is True
    # Viewers do not see the request at all.
    assert all(
        i["execution_id"] != ex_id
        for i in (await client.get("/api/v1/approvals", headers=viewer["headers"])).json()["items"]
    )
    assert (
        await client.post(f"/api/v1/approvals/{item['id']}/approve", headers=viewer["headers"], json={})
    ).status_code == 404
    r = await client.post(
        f"/api/v1/approvals/{item['id']}/comment", headers=approver["headers"], json={"body": "Checking"}
    )
    assert r.status_code == 201
    r = await client.post(
        f"/api/v1/approvals/{item['id']}/approve", headers=approver["headers"], json={"comment": "Valid claim"}
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert (
        await client.post(f"/api/v1/approvals/{item['id']}/approve", headers=approver["headers"], json={})
    ).status_code == 409
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "COMPLETED"
    runs = await node_runs(client, tenant, ex_id)
    assert runs["refund"]["output"] == {"refunded": 8000, "by": approver["email"]}
    assert runs["deny"]["status"] == "SKIPPED"
    detail = (await client.get(f"/api/v1/approvals/{item['id']}", headers=approver["headers"])).json()
    assert [h["action"] for h in detail["history"]] == ["requested", "comment", "approve"]


async def test_rejection_path_and_quorum(client, tenant):
    a1 = await add_user(client, tenant, "approver")
    a2 = await add_user(client, tenant, "approver")
    wf = await publish(client, tenant, approval_flow(required_approvals=2))
    ex_ok = await run(client, tenant, wf, {"amount": 9000, "customer": "A"})
    ex_no = await run(client, tenant, wf, {"amount": 9000, "customer": "B"})
    await drain()
    items = {
        i["execution_id"]: i for i in (await client.get("/api/v1/approvals", headers=a1["headers"])).json()["items"]
    }
    ok_id, no_id = items[ex_ok]["id"], items[ex_no]["id"]
    r = await client.post(f"/api/v1/approvals/{ok_id}/approve", headers=a1["headers"], json={})
    assert r.json()["status"] == "pending" and r.json()["approvals_count"] == 1
    assert (await client.post(f"/api/v1/approvals/{ok_id}/approve", headers=a1["headers"], json={})).status_code == 409
    r = await client.post(f"/api/v1/approvals/{ok_id}/approve", headers=a2["headers"], json={})
    assert r.json()["status"] == "approved"
    await client.post(f"/api/v1/approvals/{no_id}/reject", headers=a2["headers"], json={"comment": "Fraud suspected"})
    await drain()
    assert (await node_runs(client, tenant, ex_ok))["refund"]["status"] == "COMPLETED"
    runs = await node_runs(client, tenant, ex_no)
    assert runs["deny"]["output"] == {"reason": "Fraud suspected"} and runs["refund"]["status"] == "SKIPPED"


async def test_separation_of_duties(client, tenant):
    operator = await add_user(client, tenant, "operator")
    await client.post(f"/api/v1/users/{operator['id']}/roles", headers=tenant.headers, json={"role": "approver"})
    wf = await publish(client, tenant, approval_flow())
    ex_id = await run(client, tenant, wf, {"amount": 7000, "customer": "X"}, headers=operator["headers"])
    await drain()
    item = next(
        i
        for i in (await client.get("/api/v1/approvals", headers=operator["headers"])).json()["items"]
        if i["execution_id"] == ex_id
    )
    r = await client.post(f"/api/v1/approvals/{item['id']}/approve", headers=operator["headers"], json={})
    assert r.status_code == 403 and "Separation of duties" in r.json()["error"]["message"]


async def test_escalation_then_expiry(client, tenant):
    manager = await add_user(client, tenant, "approver")
    first = await add_user(client, tenant, "operator")
    wf = await publish(
        client,
        tenant,
        approval_flow(
            approvers={"user_ids": [first["id"]]},
            due_in_hours=48,
            on_timeout="reject",
            escalation={"after_hours": 4, "escalate_to": {"user_ids": [manager["id"]]}, "max_escalations": 1},
        ),
    )
    ex_id = await run(client, tenant, wf, {"amount": 6000, "customer": "Slow Co"})
    await drain()
    assert all(
        i["execution_id"] != ex_id
        for i in (await client.get("/api/v1/approvals", headers=manager["headers"])).json()["items"]
    )
    await fast_forward(ex_id)  # escalation deadline passes
    await drain()
    item = next(
        i
        for i in (await client.get("/api/v1/approvals", headers=manager["headers"])).json()["items"]
        if i["execution_id"] == ex_id
    )
    assert item["escalation_level"] == 1
    await fast_forward(ex_id)  # final deadline passes with no decision
    await drain()
    ex = await get_execution(client, tenant, ex_id)
    assert ex["status"] == "COMPLETED"
    runs = await node_runs(client, tenant, ex_id)
    assert runs["approval"]["output"]["decision"] == "rejected" and runs["deny"]["status"] == "COMPLETED"


async def test_request_info_form(client, tenant):
    agent = await add_user(client, tenant, "operator")
    d = definition(
        [
            node("t", "trigger.manual"),
            node(
                "ask",
                "human.request_info",
                {
                    "title": "Need PO number",
                    "approvers": {"user_ids": [agent["id"]]},
                    "fields": [{"name": "po_number", "type": "string"}, {"name": "amount", "type": "number"}],
                },
            ),
            node("use", "logic.set_variable", {"assignments": {"po": "{{ nodes.ask.output.data.po_number }}"}}),
        ],
        [edge("t", "ask"), edge("ask", "use")],
    )
    wf = await publish(client, tenant, d)
    ex_id = await run(client, tenant, wf)
    await drain()
    item = next(
        i
        for i in (await client.get("/api/v1/approvals", headers=agent["headers"])).json()["items"]
        if i["execution_id"] == ex_id
    )
    assert item["form_schema"]["required"] == ["po_number", "amount"]
    bad = await client.post(
        f"/api/v1/approvals/{item['id']}/approve",
        headers=agent["headers"],
        json={"response_data": {"po_number": "PO-1"}},
    )
    assert bad.status_code == 422
    ok = await client.post(
        f"/api/v1/approvals/{item['id']}/approve",
        headers=agent["headers"],
        json={"response_data": {"po_number": "PO-77", "amount": 120}},
    )
    assert ok.status_code == 200
    await drain()
    assert (await node_runs(client, tenant, ex_id))["use"]["output"] == {"po": "PO-77"}


async def test_cancel_cancels_pending_approval(client, tenant):
    approver = await add_user(client, tenant, "approver")
    wf = await publish(client, tenant, approval_flow())
    ex_id = await run(client, tenant, wf, {"amount": 9999, "customer": "Z"})
    await drain()
    await client.post(f"/api/v1/executions/{ex_id}/cancel", headers=tenant.headers)
    items = (await client.get("/api/v1/approvals", headers=approver["headers"], params={"status": "all"})).json()[
        "items"
    ]
    assert next(i for i in items if i["execution_id"] == ex_id)["status"] == "cancelled"
