from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import audit, auth, tenancy

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(tenancy.router)
api_router.include_router(audit.router)
