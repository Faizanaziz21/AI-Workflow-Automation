from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import ai, approvals, audit, auth, connections, executions, templates, tenancy, triggers, workflows

api_router = APIRouter()
for module in (ai, auth, tenancy, audit, connections, workflows, executions, approvals, triggers, templates):
    api_router.include_router(module.router)
