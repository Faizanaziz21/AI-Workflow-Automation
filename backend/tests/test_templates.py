from __future__ import annotations

import uuid

import pytest

from app.engine.validation import validate_definition
from app.templates.builder import placeholders, substitute
from app.templates.library import all_templates


@pytest.mark.parametrize("spec", all_templates(), ids=lambda s: s.slug)
def test_template_is_valid_once_connections_are_mapped(spec):
    definition = spec.definition()
    roles = placeholders(definition)
    assert roles <= set(spec.connection_roles), roles - set(spec.connection_roles)
    report = validate_definition(substitute(definition, {r: str(uuid.uuid4()) for r in roles}))
    assert report.valid, report.as_dict()
    assert not report.as_dict()["warnings"]


def test_ten_templates_with_unique_slugs():
    slugs = [t.slug for t in all_templates()]
    assert len(slugs) == 10 and len(set(slugs)) == 10


async def test_install_without_connections_requires_selection(client, tenant):
    from app.db.session import get_sessionmaker
    from app.services.templates import seed_templates

    async with get_sessionmaker()() as s:
        await seed_templates(s)
        await s.commit()
    r = await client.post(
        "/api/v1/templates/invoice-processing/install",
        headers=tenant.headers,
        json={"workspace_id": tenant.workspace_id},
    )
    assert r.status_code == 201
    wf = r.json()["workflow_id"]
    report = (await client.post(tenant.ws(f"/workflows/{wf}/validate"), headers=tenant.headers)).json()
    assert not report["valid"]
    assert {e["message"] for e in report["errors"]} == {"Select a connection"}
    r = await client.post(
        "/api/v1/templates/invoice-processing/install",
        headers=tenant.headers,
        json={"workspace_id": tenant.workspace_id, "connections": {"erp": str(uuid.uuid4())}},
    )
    assert r.status_code == 422
    detail = (await client.get("/api/v1/templates/customer-support-automation", headers=tenant.headers)).json()
    assert detail["node_count"] == 23 and "human.manual_review" in detail["node_types"]
