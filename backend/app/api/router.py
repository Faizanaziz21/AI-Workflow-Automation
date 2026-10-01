from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import approvals, audit, auth, connections, executions, tenancy, triggers, workflows

api_router = APIRouter()
for module in (auth, tenancy, audit, connections, workflows, executions, approvals, triggers):
    api_router.include_router(module.router)
