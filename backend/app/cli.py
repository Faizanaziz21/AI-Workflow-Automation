"""Operational CLI.

python -m app.cli seed-demo --sandbox-url http://sandbox:9000 [--executions 40]
python -m app.cli create-org --name "Acme" --email admin@acme.com --password '...'
python -m app.cli rewrap-secrets --org-slug acme      # after rotating FF_ACTIVE_ENCRYPTION_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import uuid
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.enums import Role
from app.db.models import Organization, RoleAssignment, Team, TeamMember, User, Workflow, Workspace
from app.db.session import get_sessionmaker

DEMO_PASSWORD = "FlowForge-Demo-2026!"
DEMO_USERS = [
    ("dev@acme.example", "Dana Developer", Role.WORKFLOW_DEVELOPER),
    ("ops@acme.example", "Omar Operator", Role.OPERATOR),
    ("approver@acme.example", "Priya Approver", Role.APPROVER),
    ("viewer@acme.example", "Victor Viewer", Role.VIEWER),
    ("agent@acme.example", "Sam Support", Role.OPERATOR),
]

SAMPLE_EMAILS = [
    ("bob@initech.com", "Question about my invoice", "Hi, where can I download last month's invoice? Thanks, Bob"),
    ("amy@tinyco.io", "Password reset link expired", "Hello, my password reset link expired, can you help? Amy"),
    ("jane@globex.com", "Charged twice AGAIN", "This is unacceptable! Charged twice again. Terrible, I'm furious!"),
    ("li@umbrella.com", "SSO login loop", "Our team is stuck in an SSO login loop since this morning. Urgent please."),
    ("new.lead@hooli.com", "Pricing for 500 seats", "We'd like a quote and a demo for 500 seats. Thanks!"),
    ("carl@vandelay.com", "Export my data", "How do I export all of our records to CSV? Appreciate it."),
    ("mia@acme-partners.com", "Partnership opportunity", "We are a reseller interested in a partnership."),
    ("joe@initech.com", "Refund request - legal", "I will involve our lawyer if the refund is not processed."),
]


async def create_org(name: str, email: str, password: str) -> tuple[Organization, User]:
    from app.services.auth import register_organization

    async with get_sessionmaker()() as session:
        org, user = await register_organization(
            session, org_name=name, email=email, full_name="Administrator", password=password
        )
        await session.commit()
        return org, user


async def seed_demo(sandbox_url: str, executions: int) -> dict[str, Any]:
    from app.core.security import hash_password
    from app.demo import provision_demo_connections
    from app.engine import executor
    from app.services import workflows as wf_service
    from app.services.auth import load_user_principal
    from app.services.templates import install, seed_templates
    from app.templates.builder import placeholders

    sm = get_sessionmaker()
    async with sm() as session:
        admin = (await session.execute(select(User).where(User.email == "admin@acme.example"))).scalar_one_or_none()
    if admin is None:
        _, admin = await create_org("Acme Corporation", "admin@acme.example", DEMO_PASSWORD)
    async with sm() as session:
        await seed_templates(session)
        org_id = admin.org_id
        ws = (
            (await session.execute(select(Workspace).where(Workspace.org_id == org_id).order_by(Workspace.created_at)))
            .scalars()
            .first()
        )
        assert ws is not None
        ws.name, ws.description = "Customer Operations", "Support, sales and success automation"
        finance = (
            await session.execute(select(Workspace).where(Workspace.org_id == org_id, Workspace.slug == "finance"))
        ).scalar_one_or_none()
        if finance is None:
            session.add(
                Workspace(org_id=org_id, name="Finance", slug="finance", description="AP, procurement, reporting")
            )
        team = (
            await session.execute(select(Team).where(Team.org_id == org_id, Team.name == "Support Agents"))
        ).scalar_one_or_none()
        if team is None:
            team = Team(org_id=org_id, name="Support Agents", description="Tier-1 and tier-2 support")
            session.add(team)
        await session.flush()
        for email, name, role in DEMO_USERS:
            user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if user is None:
                user = User(org_id=org_id, email=email, full_name=name, password_hash=hash_password(DEMO_PASSWORD))
                session.add(user)
                await session.flush()
                session.add(RoleAssignment(org_id=org_id, user_id=user.id, role=role.value))
                if "agent" in email:
                    session.add(TeamMember(team_id=team.id, user_id=user.id, org_id=org_id))
        await session.commit()

    async with sm() as session:
        principal = await load_user_principal(session, admin.id, admin.org_id)
        roles = await provision_demo_connections(session, principal, ws.id, sandbox_url)
        await session.commit()

    installed: dict[str, uuid.UUID] = {}
    async with sm() as session:
        principal = await load_user_principal(session, admin.id, admin.org_id)
        from app.db.models import WorkflowTemplate

        for template in (await session.execute(select(WorkflowTemplate))).scalars().all():
            existing = (
                (
                    await session.execute(
                        select(Workflow).where(Workflow.workspace_id == ws.id, Workflow.template_slug == template.slug)
                    )
                )
                .scalars()
                .first()
            )
            if existing:
                installed[template.slug] = existing.id
                continue
            needed = placeholders(template.definition)
            wf = await install(
                session,
                principal,
                template.slug,
                workspace_id=ws.id,
                name=None,
                connections={r: roles[r] for r in needed if r in roles},
            )
            installed[template.slug] = wf.id
            if needed <= set(roles):
                try:
                    async with session.begin_nested():
                        await wf_service.publish(session, principal, wf, "Initial publish (demo seed)")
                except Exception as exc:  # templates needing drive/teams/sms stay drafts until connected
                    print(f"  draft only: {template.slug}: {exc}", file=sys.stderr)
        await session.commit()

    started = 0
    if executions:
        async with sm() as session:
            wf = await session.get(Workflow, installed["customer-support-automation"])
            assert wf is not None and wf.published_version_id
            rng = random.Random(7)  # noqa: S311 - demo data
            for i in range(executions):
                sender, subject, body = rng.choice(SAMPLE_EMAILS)
                await executor.start_execution(
                    session,
                    org_id=wf.org_id,
                    workspace_id=wf.workspace_id,
                    workflow_id=wf.id,
                    version_id=wf.published_version_id,
                    trigger_type="webhook",
                    payload={
                        "body": {"from": sender, "subject": subject, "text": body},
                        "headers": {},
                        "query": {},
                        "method": "POST",
                    },
                    idempotency_key=f"demo-seed-{i}",
                    correlation_key=sender,
                )
                started += 1
            await session.commit()
    return {
        "org_id": str(admin.org_id),
        "workspace_id": str(ws.id),
        "connections": roles,
        "workflows": {k: str(v) for k, v in installed.items()},
        "executions_started": started,
        "login": {"email": "admin@acme.example", "password": DEMO_PASSWORD},
    }


async def rewrap(org_slug: str) -> int:
    from app.services.auth import load_user_principal
    from app.services.connections import rewrap_org_secrets

    async with get_sessionmaker()() as session:
        org = (await session.execute(select(Organization).where(Organization.slug == org_slug))).scalar_one()
        admin = (
            (
                await session.execute(
                    select(User)
                    .join(RoleAssignment)
                    .where(User.org_id == org.id, RoleAssignment.role == Role.ORG_ADMIN.value)
                )
            )
            .scalars()
            .first()
        )
        assert admin is not None
        principal = await load_user_principal(session, admin.id, org.id)
        count = await rewrap_org_secrets(session, principal)
        await session.commit()
        return count


def main() -> None:
    configure_logging(get_settings().log_level, json_output=False)
    parser = argparse.ArgumentParser(prog="flowforge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("seed-demo")
    p.add_argument("--sandbox-url", default="http://sandbox:9000")
    p.add_argument("--executions", type=int, default=0)
    p = sub.add_parser("create-org")
    p.add_argument("--name", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p = sub.add_parser("rewrap-secrets")
    p.add_argument("--org-slug", required=True)
    args = parser.parse_args()
    if args.cmd == "seed-demo":
        result = asyncio.run(seed_demo(args.sandbox_url, args.executions))
        import json

        print(json.dumps(result, indent=2))
    elif args.cmd == "create-org":
        org, user = asyncio.run(create_org(args.name, args.email, args.password))
        print(f"Created organization {org.slug} ({org.id}) with admin {user.email}")
    elif args.cmd == "rewrap-secrets":
        print(f"Re-wrapped {asyncio.run(rewrap(args.org_slug))} secrets")


if __name__ == "__main__":
    main()
